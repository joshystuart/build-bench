"""The live display `run` draws in a terminal: one progress bar per Trial.

Nothing reports to it. Each frame is read from the files the Trials already
write: status.json, pre_build.log, stream.jsonl and the plan in the workspace.

How far a Trial has got is not known in time, so the best signal there is is
used, and named beside the bar:

1. the agent's own todo list (TodoWrite, or TaskCreate and TaskUpdate);
2. the boxes ticked in the plan inside the workspace;
3. its tool calls so far against the median of finished Trials;
4. otherwise nothing, and only its limits are shown: time against the
   timeout and cost against the budget.

Time left for the Experiment comes from how long finished Trials took; until
one has finished, only an upper bound from the timeout can be given.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import statistics
import sys
import threading
import time
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .agent import USD_PER_NANO_AIU
from .results import Trial, read_json
from .settings import Price, Settings
from .transcript import default_prices, load_jsonl, messages, tokens_and_cost

#: Seconds without a new event before a running Trial is shown as quiet.
STALL_SECONDS = 180
#: Seconds between frames.
INTERVAL = 1.0
BAR = 30
LIMIT_BAR = 10

#: (text, style); style is a key of STYLES or "".
Line = tuple[str, str]
STYLES = {"bold": "1", "dim": "2", "green": "32", "red": "31", "yellow": "33", "cyan": "36"}


@dataclass
class Stream:
    """What a Trial's stream.jsonl says so far."""

    tools: int = 0
    last: str = ""
    cost: float = 0.0
    todos: tuple[int, int] | None = None
    subagents: dict[str, str] = field(default_factory=dict)
    ended: bool = False


@dataclass
class Progress:
    """One Trial as it stands now."""

    trial: Trial
    state: str = "waiting"  # waiting | running | finished | failed
    reason: str = ""
    started: float | None = None
    #: When the agent was first seen running; its timeout counts from then.
    agent_started: float | None = None
    seconds: float | None = None
    phase: str = ""
    stream: Stream = field(default_factory=Stream)
    last_at: float | None = None
    plan: tuple[int, int] | None = None
    fraction: float | None = None
    basis: str = ""


def describe(block: dict) -> str:
    """A tool call in a few words: its name and what it works on."""
    name, given = block.get("name", "?"), block.get("input") or {}
    if name == "Bash":
        what = given.get("description") or given.get("command", "")
    elif name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        what = Path(str(given.get("file_path", ""))).name
    elif name in ("Grep", "Glob"):
        what = given.get("pattern", "")
    elif name in ("Agent", "Task"):
        what = given.get("description", "")
    else:
        what = ""
    return " ".join(f"{name} {what}".split())[:60]


def read_copilot_stream(entries: list[dict]) -> Stream:
    """The Copilot CLI's stream: tool calls, running subagents and the end. It has
    no todo list, and what the run costs is only known once it ends."""
    found = Stream()
    for entry in entries:
        kind, data = entry.get("type"), entry.get("data") or {}
        if kind == "tool.execution_start":
            name, given = str(data.get("toolName", "?")), data.get("arguments") or {}
            what = given.get("description") or given.get("command") or given.get("path") or ""
            said = " ".join(f"{name} {Path(str(what)).name if name != 'bash' else what}".split())
            if entry.get("agentId"):
                found.last = "subagent: " + said[:60]
                continue
            found.tools += 1
            found.last = said[:60]
        elif kind == "subagent.started":
            found.subagents[str(entry.get("agentId"))] = str(data.get("agentDisplayName", ""))
        elif kind in ("subagent.completed", "subagent.failed"):
            found.subagents.pop(str(entry.get("agentId")), None)
        elif kind == "session.usage_checkpoint" and data.get("totalNanoAiu"):
            found.cost = data["totalNanoAiu"] * USD_PER_NANO_AIU
        elif kind == "result":
            found.ended = True
    return found


def read_stream(path: Path, prices: dict[str, Price]) -> Stream:
    entries = load_jsonl(path)
    if any(entry.get("type") == "session.start" for entry in entries):
        return read_copilot_stream(entries)
    found = Stream()
    made = completed = 0
    for entry in entries:
        kind, subtype = entry.get("type"), entry.get("subtype")
        if kind == "assistant":
            content = (entry.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if entry.get("parent_tool_use_id"):
                    found.last = "subagent: " + describe(block)
                    continue
                found.tools += 1
                found.last = describe(block)
                given = block.get("input") or {}
                if block.get("name") == "TodoWrite" and isinstance(given.get("todos"), list):
                    todos = given["todos"]
                    done = sum(
                        isinstance(t, dict) and t.get("status") == "completed" for t in todos
                    )
                    found.todos = (done, len(todos)) if todos else None
                elif block.get("name") == "TaskCreate":
                    made += 1
                elif block.get("name") == "TaskUpdate" and given.get("status") == "completed":
                    completed += 1
        elif kind == "system" and subtype == "task_started":
            found.subagents[str(entry.get("task_id"))] = str(entry.get("description", ""))
        elif kind == "system" and subtype == "task_notification":
            found.subagents.pop(str(entry.get("task_id")), None)
        elif kind == "result":
            found.ended = True
    if made and found.todos is None:
        found.todos = (min(completed, made), made)
    found.cost = tokens_and_cost(messages(entries), prices)[1]
    return found


def checkboxes(folder: Path) -> tuple[int, int] | None:
    """Boxes ticked and boxes in all, across the Markdown files of a plan."""
    ticked = total = 0
    for md in sorted(folder.rglob("*.md")) if folder.is_dir() else []:
        try:
            text = md.read_text(errors="replace")
        except OSError:
            continue
        for mark in re.findall(r"^\s*[-*] \[([ xX])\]", text, re.M):
            total += 1
            ticked += mark != " "
    return (ticked, total) if total else None


class Reader:
    """Reads each Trial's progress, parsing a stream again only when it has grown."""

    def __init__(
        self, settings: Settings, redo: AbstractSet[str] = frozenset(), since: float = 0.0
    ):
        self.settings = settings
        self.prices = {**default_prices(), **settings.prices}
        #: Trials this run builds again: until they start, an older status is stale.
        self.redo, self.since = redo, since
        self._streams: dict[Path, tuple[tuple[int, int], Stream]] = {}
        self._agent_seen: dict[Path, float] = {}

    def stream(self, path: Path) -> Stream:
        stat = path.stat()
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._streams.get(path)
        if cached is None or cached[0] != key:
            cached = (key, read_stream(path, self.prices))
            self._streams[path] = cached
        return cached[1]

    def read(self, trial: Trial) -> Progress:
        found = Progress(trial)
        status_file = trial.folder / "status.json"
        status = read_json(status_file)
        if not status or (trial.id in self.redo and status_file.stat().st_mtime < self.since):
            return found
        found.state = status.get("status", "running")
        found.reason = status.get("reason") or ""
        stream_file, pre_build = trial.folder / "stream.jsonl", trial.folder / "pre_build.log"
        if found.state == "running":
            found.started = status_file.stat().st_mtime
            found.last_at = found.started
        else:
            timings = read_json(trial.folder / "timings.json") or {}
            found.seconds = (timings.get("pre_build_seconds") or 0) + (
                timings.get("agent_seconds") or 0
            )
        if stream_file.exists():
            found.stream = self.stream(stream_file)
            found.last_at = stream_file.stat().st_mtime
            if found.state == "running":
                found.agent_started = self._agent_seen.setdefault(stream_file, time.time())
            found.phase = "saving the diff" if found.stream.ended else "agent"
        elif pre_build.exists():
            steps = [line[2:] for line in pre_build.read_text(errors="replace").splitlines()
                     if line.startswith("$ ")]  # fmt: skip
            found.phase = f"pre_build: {steps[-1]}" if steps else "pre_build"
            found.last_at = pre_build.stat().st_mtime
        else:
            found.phase = "making the workspace"
        if found.state != "running":
            self._agent_seen.pop(stream_file, None)
        if found.state == "running" and self.settings.plan_rel:
            found.plan = checkboxes(trial.workspace / self.settings.plan_rel)
        return found


def _typical(done: list[Progress], variant: str, value) -> tuple[float, int] | None:
    """The median of a value over finished Trials of a Variant, or of any Variant."""
    same = [value(p) for p in done if p.trial.variant == variant]
    pool = same or [value(p) for p in done]
    return (statistics.median(pool), len(pool)) if pool else None


def estimate(trials: list[Progress]) -> None:
    """Fill in how far each running Trial has got, and on what that is based."""
    done = [p for p in trials if p.state == "finished"]
    for p in trials:
        if p.state != "running":
            continue
        tools = _typical(done, p.trial.variant, lambda d: d.stream.tools)
        if p.stream.ended:
            p.fraction, p.basis = 1.0, p.phase
        elif p.phase != "agent":
            p.basis = p.phase
        elif p.stream.todos:
            p.fraction = p.stream.todos[0] / p.stream.todos[1]
            p.basis = f"todo list {p.stream.todos[0]}/{p.stream.todos[1]}"
        elif p.plan:
            p.fraction = p.plan[0] / p.plan[1]
            p.basis = f"plan {p.plan[0]}/{p.plan[1]} ticked"
        elif tools and tools[0] > 0:
            p.fraction = min(p.stream.tools / tools[0], 0.95)
            p.basis = f"{p.stream.tools} of ~{tools[0]:.0f} tool calls"
        else:
            p.basis = "no estimate until a Trial finishes"


def time_left(trials: list[Progress], settings: Settings, now: float) -> str:
    done = [p for p in trials if p.state == "finished"]
    left = [p for p in trials if p.state in ("waiting", "running")]
    if not left:
        return "all Trials ended"
    timeout = settings.limits.timeout_minutes
    if not done:
        if timeout:
            waves = math.ceil(len(left) / settings.concurrency)
            most = clock(waves * timeout * 60)
            return f"time left: unknown until a Trial finishes (timeouts allow agents up to {most})"
        return "time left: unknown until a Trial finishes"
    seconds = 0.0
    for p in left:
        typical = _typical(done, p.trial.variant, lambda d: d.seconds or 0)
        assert typical is not None
        if p.state == "waiting":
            seconds += typical[0]
            continue
        elapsed = now - (p.started or now)
        if p.fraction:
            seconds += max(elapsed / p.fraction - elapsed, 0)
        else:
            seconds += max(typical[0] - elapsed, 0)
    return (
        f"time left: ~{clock(seconds / settings.concurrency)} (from {len(done)} finished Trial(s))"
    )


def clock(seconds: float | None) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def bar(fraction: float | None, width: int) -> str:
    if fraction is None:
        return "·" * width
    full = round(max(0.0, min(fraction, 1.0)) * width)
    return "█" * full + "░" * (width - full)


def _limit(name: str, used: float, cap: float | None, show) -> str:
    """How much of a limit is used, as a small bar when there is a limit."""
    if not cap:
        return f"{name} {show(used)}"
    return f"{name} [{bar(used / cap, LIMIT_BAR)}] {show(used)}/{show(cap)}"


def render(
    settings: Settings,
    trials: list[Progress],
    started: float,
    now: float,
    height: int,
    hint: str = "",
) -> list[Line]:
    """The frame: the Experiment's bar, then each Trial's, in at most `height` lines.
    A `hint` (how to stop) goes under the Experiment's bar."""
    estimate(trials)
    pad = max(len(p.trial.id) for p in trials) + 2
    count = {s: sum(p.state == s for p in trials) for s in ("finished", "running", "failed")}
    ended = count["finished"] + count["failed"]
    partial = sum(p.fraction or 0 for p in trials if p.state == "running")
    head: list[Line] = [
        (
            f"{'Experiment':<{pad}}[{bar((ended + partial) / len(trials), BAR)}]  "
            f"{count['finished']}/{len(trials)} done · {count['running']} running · "
            f"{count['failed']} failed · {clock(now - started)} elapsed",
            "bold",
        ),
        (" " * pad + time_left(trials, settings, now), "cyan"),
    ]
    if hint and count["running"] + sum(p.state == "waiting" for p in trials):
        head.append((" " * pad + hint, "dim"))
    running: list[Line] = []
    finished: list[Line] = []
    waiting = [p.trial.id for p in trials if p.state == "waiting"]
    budget, timeout = settings.limits.max_budget_usd, settings.limits.timeout_minutes
    for p in trials:
        if p.state == "running":
            quiet = now - (p.last_at or now)
            style = "yellow" if quiet >= STALL_SECONDS else ""
            pct = f"{p.fraction:4.0%}" if p.fraction is not None else "   ?"
            running.append((f"{p.trial.id:<{pad}}[{bar(p.fraction, BAR)}] {pct}  {p.basis}", style))
            if p.agent_started is not None and not p.stream.ended:
                agent = now - p.agent_started
                limits = [_limit("agent time", agent, timeout and timeout * 60, clock)]
            else:
                limits = [f"time {clock(now - (p.started or now))}"]
            if p.phase == "agent" or p.stream.cost:
                limits.append(_limit("cost", p.stream.cost, budget, lambda d: f"${d:.2f}"))
            running.append((" " * pad + "   ".join(limits), "dim"))
            doing = p.stream.last if p.phase == "agent" else p.phase
            if p.phase == "agent" and not doing:
                doing = "waiting for the agent's first step"
            if p.stream.subagents:
                doing = f"{len(p.stream.subagents)} subagent(s) running · {doing}"
            when = f"quiet for {clock(quiet)}" if style else f"{clock(quiet)} ago"
            running.append((" " * pad + f"now: {doing} · {when}", style or "dim"))
        elif p.state == "finished":
            finished.append(
                (f"{p.trial.id:<{pad}}[{bar(1, BAR)}] done in {clock(p.seconds)}", "green")
            )
        elif p.state == "failed":
            finished.append((f"{p.trial.id:<{pad}}[{bar(0, BAR)}] failed: {p.reason}", "red"))
    tail: list[Line] = [(" " * pad + "waiting: " + " ".join(waiting), "dim")] if waiting else []
    lines = head + running + finished + tail
    if len(lines) > height and finished:
        names = {"green": "finished", "red": "failed"}
        summary = " · ".join(
            f"{names[style]}: " + " ".join(text.split()[0] for text, s in finished if s == style)
            for style in ("green", "red")
            if any(s == style for _, s in finished)
        )
        lines = head + running + [(" " * pad + summary, "dim")] + tail
    if len(lines) > height:
        lines = lines[: max(height - 1, 1)] + [("…", "dim")]
    return lines


class Live:
    """Redraws the frame in place while Trials run; lines it is given print above it."""

    def __init__(
        self,
        settings: Settings,
        trials: list[Trial],
        redo: list[Trial],
        out: TextIO | None = None,
        hint: str = "",
    ):
        self.settings = settings
        self.hint = hint
        self.trials = trials
        self.started = time.time()
        self.reader = Reader(settings, {t.id for t in redo}, self.started)
        self.out = out or sys.stdout
        self.color = not os.environ.get("NO_COLOR")
        self._drawn = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> Live:
        with self._lock:
            self._draw()
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        with self._lock:
            self._clear()
            self._draw()

    def say(self, text: str) -> None:
        with self._lock:
            self._clear()
            self.out.write(text + "\n")
            self._draw()

    def _loop(self) -> None:
        while not self._stop.wait(INTERVAL):
            with self._lock:
                self._clear()
                self._draw()

    def _clear(self) -> None:
        if self._drawn:
            # To the start of the frame's first line, then clear to the end of the screen.
            self.out.write(f"\x1b[{self._drawn}F\x1b[J")
            self._drawn = 0

    def _draw(self) -> None:
        width, height = shutil.get_terminal_size()
        now = time.time()
        try:
            progress = [self.reader.read(t) for t in self.trials]
            lines = render(
                self.settings, progress, self.started, now, max(height - 1, 3), self.hint
            )
        except Exception:  # noqa: BLE001 - the display must never stop a run
            # Most often a file went away between two reads (a failed Trial
            # starting again); the next frame will find it.
            return
        for text, style in lines:
            text = text[: max(width - 1, 1)]
            if style and self.color:
                text = f"\x1b[{STYLES[style]}m{text}\x1b[0m"
            self.out.write(text + "\n")
        self.out.flush()
        self._drawn = len(lines)
