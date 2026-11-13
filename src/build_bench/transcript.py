"""Measurements read from a Trial's transcript: cost, tokens and time.

A Claude Code transcript is JSON lines. One model message is often written as
several lines (one per content block) that repeat the same usage, so usage is
counted once per message id. Subagents write their own files under
``subagents/``, each with a ``.meta.json`` naming the Agent tool call that
started it.
"""

from __future__ import annotations

import json
import shlex
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .settings import Price

#: Tools that start subagents; their time is waiting, not tool work.
AGENT_TOOLS = {"Agent", "Task"}
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write")


def default_prices() -> dict[str, Price]:
    text = (resources.files("build_bench") / "prices.yaml").read_text()
    return {model: Price(**row) for model, row in yaml.safe_load(text).items()}


def price_for(model: str, prices: dict[str, Price]) -> Price | None:
    matches = [key for key in prices if model.startswith(key)]
    return prices[max(matches, key=len)] if matches else None


def load_jsonl(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _time(entry: dict) -> float | None:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def messages(entries: list[dict]) -> dict[str, tuple[str, dict]]:
    """Model and usage for each model message, once per message id."""
    found: dict[str, tuple[str, dict]] = {}
    for entry in entries:
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict) or message.get("model") == "<synthetic>":
            continue
        key = message.get("id") or entry.get("uuid") or ""
        found[key] = (message.get("model") or "unknown", usage)
    return found


def tokens_and_cost(
    msgs: dict[str, tuple[str, dict]], prices: dict[str, Price]
) -> tuple[dict[str, int], float, list[str]]:
    tokens = dict.fromkeys(TOKEN_KINDS, 0)
    cost = 0.0
    unpriced: set[str] = set()
    for model, usage in msgs.values():
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        read = int(usage.get("cache_read_input_tokens") or 0)
        write = int(usage.get("cache_creation_input_tokens") or 0)
        split = usage.get("cache_creation") or {}
        write_1h = int(split.get("ephemeral_1h_input_tokens") or 0)
        write_5m = write - write_1h
        tokens["input"] += inp
        tokens["output"] += out
        tokens["cache_read"] += read
        tokens["cache_write"] += write
        price = price_for(model, prices)
        if price is None:
            unpriced.add(model)
            continue
        cost += (
            inp * price.input
            + out * price.output
            + read * (price.cache_read if price.cache_read is not None else price.input * 0.1)
            + write_5m
            * (price.cache_write_5m if price.cache_write_5m is not None else price.input * 1.25)
            + write_1h
            * (price.cache_write_1h if price.cache_write_1h is not None else price.input * 2)
        ) / 1_000_000
    tokens["total"] = sum(tokens[k] for k in TOKEN_KINDS)
    return tokens, cost, sorted(unpriced)


def tool_calls(entries: list[dict]) -> list[dict]:
    """Each tool call with its name, input, start and end time."""
    calls: dict[str, dict] = {}
    for entry in entries:
        message = entry.get("message")
        # Only the conversation itself: other lines (such as `api-request-blob`)
        # replay earlier tool calls with no time of their own.
        if entry.get("type") not in ("assistant", "user") or not isinstance(message, dict):
            continue
        if not isinstance(message.get("content"), list):
            continue
        when = _time(entry)
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id") and block["id"] not in calls:
                calls[block["id"]] = {
                    "id": block["id"],
                    "name": block.get("name", "?"),
                    "input": block.get("input") or {},
                    "start": when,
                    "end": None,
                }
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                calls[block["tool_use_id"]]["end"] = when
    return list(calls.values())


def command_kind(command: str) -> str:
    """A shell command's first word, after `cd somewhere &&` and `VAR=value`."""
    segments = [s.strip() for s in command.replace("&&", "\n").replace(";", "\n").splitlines()]
    for segment in segments:
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        words = [w for w in words if not (("=" in w) and w.split("=")[0].isidentifier())]
        if words and words[0] != "cd":
            return Path(words[0]).name
    return "cd" if segments and segments[0].startswith("cd") else "?"


def tool_kind(call: dict) -> str:
    if call["name"] == "Bash":
        return f"Bash: {command_kind(str(call['input'].get('command', '')))}"
    return call["name"]


def merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def overlap(
    a: list[tuple[float, float]], b: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The parts of intervals ``a`` that fall inside intervals ``b``."""
    found = []
    for a_start, a_end in a:
        for b_start, b_end in b:
            start, end = max(a_start, b_start), min(a_end, b_end)
            if start < end:
                found.append((start, end))
    return found


def idle_gaps(entries: list[dict]) -> list[tuple[float, float]]:
    """Gaps where the main agent had ended its turn and was waiting for a new
    message, such as the notice that a background subagent finished."""
    gaps = []
    last = None
    for entry in entries:
        when = _time(entry)
        if entry.get("type") not in ("assistant", "user") or when is None:
            continue
        content = (entry.get("message") or {}).get("content")
        is_prompt = entry["type"] == "user" and not (
            isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        )
        if is_prompt and last is not None and when > last:
            gaps.append((last, when))
        last = when
    return gaps


def measure(transcript: Path, prices: dict[str, Price]) -> dict[str, Any]:
    """Every transcript measurement for one Trial. ``transcript`` holds main.jsonl."""
    main = load_jsonl(transcript / "main.jsonl")
    times = [t for t in map(_time, main) if t is not None]
    if not times:
        raise ValueError(f"no timed entries in {transcript / 'main.jsonl'}")
    origin = min(times)

    def rel(t: float | None) -> float | None:
        return None if t is None else round(t - origin, 3)

    main_msgs = messages(main)
    all_msgs = dict(main_msgs)
    calls = tool_calls(main)
    main_tokens, main_cost, unpriced = tokens_and_cost(main_msgs, prices)
    agents = [{"agent": "main", "tokens": main_tokens, "cost_usd": main_cost}]

    subagents: list[dict[str, Any]] = []
    sub_folder = transcript / "subagents"
    for path in sorted(sub_folder.glob("*.jsonl")) if sub_folder.is_dir() else []:
        entries = load_jsonl(path)
        meta = json.loads(m.read_text()) if (m := path.with_suffix(".meta.json")).is_file() else {}
        msgs = messages(entries)
        all_msgs.update(msgs)
        tokens, cost, missing = tokens_and_cost(msgs, prices)
        unpriced = sorted(set(unpriced) | set(missing))
        calls += tool_calls(entries)
        sub_times = [t for t in map(_time, entries) if t is not None]
        name = meta.get("description") or path.stem
        subagents.append(
            {
                "id": path.stem,
                "name": name,
                "type": meta.get("agentType"),
                "_start": min(sub_times) if sub_times else None,
                "_end": max(sub_times) if sub_times else None,
                "start": rel(min(sub_times)) if sub_times else None,
                "end": rel(max(sub_times)) if sub_times else None,
                "seconds": round(max(sub_times) - min(sub_times), 3) if sub_times else 0.0,
                "tokens": tokens,
                "cost_usd": cost,
                "tool_use_id": meta.get("toolUseId"),
                "background": meta.get("requestShape") == "background",
            }
        )
        agents.append({"agent": name, "tokens": tokens, "cost_usd": cost})

    tokens, cost, _ = tokens_and_cost(all_msgs, prices)
    # Blocking Agent calls, plus the main agent's idle gaps while a background
    # subagent is still running (it ends its turn and is woken when one finishes).
    blocking = [
        (c["start"], c["end"])
        for c in tool_calls(main)
        if c["name"] in AGENT_TOOLS and c["start"] is not None and c["end"] is not None
    ]
    running = merge([(s["_start"], s["_end"]) for s in subagents if s.get("_start") is not None])
    waits = merge(blocking + overlap(idle_gaps(main), running))
    tool_seconds: dict[str, float] = {}
    for call in calls:
        if call["name"] in AGENT_TOOLS or call["start"] is None or call["end"] is None:
            continue
        kind = tool_kind(call)
        tool_seconds[kind] = tool_seconds.get(kind, 0.0) + (call["end"] - call["start"])

    for sub in subagents:
        del sub["_start"], sub["_end"]
    return {
        "cost_usd": cost,
        "tokens": tokens,
        "unpriced_models": unpriced,
        "by_agent": agents,
        "total_seconds": round(max(times) - origin, 3),
        "wait_seconds": round(sum(e - s for s, e in waits), 3),
        "wait_intervals": [[rel(s), rel(e)] for s, e in waits],
        "subagents": subagents,
        "tool_seconds": {k: round(v, 3) for k, v in sorted(tool_seconds.items())},
    }
