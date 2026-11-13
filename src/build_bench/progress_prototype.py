"""PROTOTYPE, throwaway. Not part of build-bench; delete once a variant is picked.

Question: what should `build-bench run` show while Trials are running, so you can
tell it is alive, how far through it is, and roughly how long is left?

Four variants of the live display, switchable with <- / -> (or 1-4) in the terminal:
  A  Event log        append-only lines, one per thing that happened (pipe/CI friendly)
  B  Board            a redrawn table, one row per Trial
  C  Bars             progress bars: how far each Trial is, and how close to its limits
  D  Log + status     milestones scroll by, a sticky status line at the bottom

Everything is read from files a real run already writes (status.json, pre_build.log,
stream.jsonl, the plan inside the workspace), so it works against a real run too:

  uv run python -m build_bench.progress_prototype                    # simulated Experiment, 20x speed
  uv run python -m build_bench.progress_prototype --speed 60 --variant C
  uv run python -m build_bench.progress_prototype --watch .bench/results/<exp>/<stamp>

"How far through" without knowing the time, best signal first:
  1. the agent's own todo list (TodoWrite / TaskCreate+TaskUpdate): done / total
  2. checkboxes ticked in the plan inside the workspace: ticked / total
  3. tool calls so far vs the median of finished Trials of the same Variant
  4. nothing yet: show only the ceilings (time vs timeout, cost vs budget)
Time left for the Experiment = median duration of finished Trials x Trials remaining
/ concurrency; before any Trial finishes, only an upper bound from the timeout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import select
import shutil
import statistics
import sys
import tempfile
import termios
import threading
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .results import read_json, workspaces, write_json
from .transcript import default_prices, load_jsonl, messages, tokens_and_cost

STALL = 180  # seconds without a new event before a Trial looks stuck
PRICES = default_prices()


class Clock:
    def __init__(self, speed: float = 1.0):
        self.real0 = time.time()
        self.speed = speed

    def now(self) -> float:
        return self.real0 + (time.time() - self.real0) * self.speed


# --------------------------------------------------------------------------- state


@dataclass
class TrialView:
    id: str
    variant: str
    state: str = "queued"  # queued | running | finished | failed
    phase: str = ""
    reason: str = ""
    started: float | None = None
    seconds: float | None = None
    tools: int = 0
    sub_tools: int = 0
    last: str = ""
    last_at: float | None = None
    cost: float = 0.0
    todos: tuple[int, int] | None = None
    plan: tuple[int, int] | None = None
    subagents: dict[str, str] = field(default_factory=dict)
    subagents_done: int = 0
    progress: float | None = None
    basis: str = ""


@dataclass
class State:
    name: str
    now: float
    started: float | None
    concurrency: int
    timeout: float | None
    budget: float | None
    trials: list[TrialView]
    eta: str = ""


def _describe(block: dict) -> str:
    name, given = block.get("name", "?"), block.get("input") or {}
    if name == "Bash":
        what = given.get("description") or given.get("command", "")
    elif name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        what = Path(given.get("file_path", "")).name
    elif name in ("Grep", "Glob"):
        what = given.get("pattern", "")
    elif name in ("Agent", "Task"):
        what = f'"{given.get("description", "")}"'
    elif name == "TodoWrite":
        what = "todo list"
    else:
        what = ""
    return f"{name} {what}".replace("\n", " ").strip()[:56]


def _checkboxes(folder: Path) -> tuple[int, int] | None:
    ticked = total = 0
    for md in folder.rglob("*.md") if folder.is_dir() else []:
        for mark in re.findall(r"^\s*[-*] \[([ xX])\]", md.read_text(errors="replace"), re.M):
            total += 1
            ticked += mark != " "
    return (ticked, total) if total else None


def _read_trial(folder: Path, view: TrialView, ws: Path | None, plan_rel: str | None) -> None:
    status_file = folder / "status.json"
    status = read_json(status_file)
    if not status:
        return
    view.state = status.get("status", "running")
    view.reason = status.get("reason") or ""
    view.started = status_file.stat().st_mtime if view.state == "running" else None
    timings = read_json(folder / "timings.json") or {}
    if view.state != "running":
        view.seconds = (timings.get("pre_build_seconds") or 0) + (
            timings.get("agent_seconds") or status.get("agent_seconds") or 0
        )

    stream, pre = folder / "stream.jsonl", folder / "pre_build.log"
    if stream.exists():
        entries = load_jsonl(stream)
        view.last_at = stream.stat().st_mtime
        tasks_made = tasks_done = 0
        ended = False
        for e in entries:
            kind, sub = e.get("type"), e.get("subtype")
            if kind == "assistant":
                content = (e.get("message") or {}).get("content")
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    if e.get("parent_tool_use_id"):
                        view.sub_tools += 1
                        view.last = "  (subagent) " + _describe(b)
                        continue
                    view.tools += 1
                    view.last = _describe(b)
                    given = b.get("input") or {}
                    if b.get("name") == "TodoWrite":
                        todos = given.get("todos") or []
                        done = sum(t.get("status") == "completed" for t in todos)
                        view.todos = (done, len(todos)) if todos else None
                    elif b.get("name") == "TaskCreate":
                        tasks_made += 1
                    elif b.get("name") == "TaskUpdate" and given.get("status") == "completed":
                        tasks_done += 1
            elif kind == "system" and sub == "task_started":
                view.subagents[e.get("task_id", "?")] = e.get("description", "subagent")
            elif kind == "system" and sub == "task_notification":
                view.subagents.pop(e.get("task_id", "?"), None)
                view.subagents_done += 1
            elif kind == "result":
                ended = True
        if tasks_made and view.todos is None:
            view.todos = (tasks_done, tasks_made)
        view.cost = tokens_and_cost(messages(entries), PRICES)[1]
        view.phase = "saving diff and transcript" if ended else "agent"
    elif pre.exists():
        steps = [
            line[2:] for line in pre.read_text(errors="replace").splitlines() if line[:2] == "$ "
        ]
        view.phase = f"pre_build: {steps[-1]}" if steps else "pre_build"
        view.last, view.last_at = view.phase, pre.stat().st_mtime
    else:
        view.phase = "cloning workspace"
        view.last, view.last_at = view.phase, status_file.stat().st_mtime
    if ws and plan_rel:
        view.plan = _checkboxes(ws / view.id / "repo" / plan_rel)


def read_state(folder: Path, now: float) -> State:
    exp = read_json(folder / "experiment.json") or {}
    settings = exp.get("settings") or {}
    limits = settings.get("limits") or {}
    ws, plan_rel = workspaces(folder), exp.get("plan")
    trials = []
    for v in settings.get("variants") or []:
        for n in range(1, int(v.get("repeats", 1)) + 1):
            view = TrialView(f"{v['name']}-{n}", v["name"])
            _read_trial(folder / "trials" / view.id, view, ws, plan_rel)
            trials.append(view)
    timeout = limits.get("timeout_minutes")
    state = State(
        exp.get("name", folder.name),
        now,
        exp.get("started_at"),
        int(settings.get("concurrency", 1)),
        timeout * 60 if timeout else None,
        limits.get("max_budget_usd"),
        trials,
    )
    _estimate(state)
    return state


def _estimate(s: State) -> None:
    done = [t for t in s.trials if t.state == "finished"]

    def typical(variant: str, attr: str) -> tuple[float, int] | None:
        same = [getattr(t, attr) for t in done if t.variant == variant]
        pool = same or [getattr(t, attr) for t in done]
        return (statistics.median(pool), len(pool)) if pool else None

    for t in s.trials:
        if t.state != "running":
            t.progress = 1.0 if t.state == "finished" else None
            continue
        tools = typical(t.variant, "tools")
        if t.todos:
            t.progress, t.basis = t.todos[0] / t.todos[1], f"todo list {t.todos[0]}/{t.todos[1]}"
        elif t.plan:
            t.progress, t.basis = t.plan[0] / t.plan[1], f"plan {t.plan[0]}/{t.plan[1]} ticked"
        elif tools and t.phase == "agent":
            t.progress = min(t.tools / max(tools[0], 1), 0.95)
            t.basis = f"{t.tools} of ~{tools[0]:.0f} tool calls ({tools[1]} done)"
        else:
            t.basis = "no estimate yet" if t.phase == "agent" else t.phase

    remaining, unknown, left_trials = 0.0, False, 0
    for t in s.trials:
        if t.state in ("finished", "failed"):
            continue
        left_trials += 1
        dur = typical(t.variant, "seconds")
        if dur is None:
            unknown = True
            continue
        if t.state == "running":
            elapsed = s.now - (t.started or s.now)
            by_progress = elapsed / t.progress - elapsed if t.progress else None
            remaining += max(by_progress if by_progress is not None else dur[0] - elapsed, 30)
        else:
            remaining += dur[0]
    if not left_trials:
        s.eta = "all Trials done"
    elif unknown and s.timeout:
        waves = math.ceil(left_trials / s.concurrency)
        s.eta = f"time left unknown until a Trial finishes; at most {fmt(waves * s.timeout)}"
    elif unknown:
        s.eta = "time left unknown until a Trial finishes"
    else:
        s.eta = f"~{fmt(remaining / s.concurrency)} left (from {len(done)} finished Trial(s))"


# --------------------------------------------------------------------------- events


@dataclass
class Event:
    at: float
    trial: str
    text: str
    milestone: bool
    style: str = ""


class EventLog:
    """Turns successive states into lines of what changed (variants A and D)."""

    def __init__(self):
        self.prev: dict[str, TrialView] = {}
        self.events: list[Event] = []
        self.beat: dict[str, float] = {}
        self.stalled: set[str] = set()

    def feed(self, s: State) -> None:
        for t in s.trials:
            p = self.prev.get(t.id) or TrialView(t.id, t.variant)

            def say(text, m=False, st="", tid=t.id):
                self.events.append(Event(s.now, tid, text, m, st))

            if t.state == "running" and p.state == "queued":
                say("started", True, "bold")
                self.beat[t.id] = s.now
            if t.state == "running" and t.phase != p.phase:
                say(t.phase, t.phase == "agent")
            if t.tools > p.tools or t.sub_tools > p.sub_tools:
                say(t.last.strip())
            for task, desc in t.subagents.items():
                if task not in p.subagents:
                    say(f"subagent started: {desc}", True)
            if t.subagents_done > p.subagents_done:
                say("subagent finished", True)
            if t.todos and t.todos != p.todos:
                say(f"todo list {t.todos[0]}/{t.todos[1]} done", True)
            if t.plan and p.plan and t.plan != p.plan:
                say(f"plan {t.plan[0]}/{t.plan[1]} ticked", True)
            if t.state == "finished" and p.state != "finished":
                say(
                    f"finished in {fmt(t.seconds)} · {t.tools} tools · ${t.cost:.2f}", True, "green"
                )
            if t.state == "failed" and p.state != "failed":
                say(f"FAILED {t.reason}", True, "red")
            quiet = s.now - (t.last_at or s.now)
            if t.state == "running" and quiet > STALL and t.id not in self.stalled:
                self.stalled.add(t.id)
                say(f"quiet for {fmt(quiet)}, last: {t.last.strip()}", True, "yellow")
            if t.state == "running" and quiet < STALL:
                self.stalled.discard(t.id)
            if t.state == "running" and s.now - self.beat.get(t.id, s.now) >= 120:
                self.beat[t.id] = s.now
                say(
                    f"still running: {fmt(s.now - (t.started or s.now))} · {t.tools} tools"
                    f" · ${t.cost:.2f} · {t.basis}",
                    st="dim",
                )
            self.prev[t.id] = t
        del self.events[:-500]


# --------------------------------------------------------------------------- rendering

STYLES = {
    "bold": "1",
    "dim": "2",
    "green": "32",
    "red": "31",
    "yellow": "33",
    "cyan": "36",
    "inverse": "7",
}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
Line = tuple[str, str]


def fmt(seconds: float | None) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def bar(frac: float | None, width: int) -> str:
    if frac is None:
        return "·" * width
    full = round(max(0.0, min(frac, 1.0)) * width)
    return "█" * full + "░" * (width - full)


def counts(s: State) -> str:
    c = {k: sum(t.state == k for t in s.trials) for k in ("finished", "running", "failed")}
    elapsed = fmt(s.now - s.started) if s.started else "?"
    return (
        f"{c['finished']}/{len(s.trials)} Trials done · {c['running']} running"
        f" · {c['failed']} failed · {elapsed} elapsed"
    )


def _style_for(t: TrialView, now: float) -> str:
    if t.state == "finished":
        return "green"
    if t.state == "failed":
        return "red"
    if t.state == "queued":
        return "dim"
    return "yellow" if now - (t.last_at or now) > STALL else ""


def view_log(s: State, log: EventLog, height: int) -> list[Line]:
    out = [
        (f"[{time.strftime('%H:%M:%S', time.localtime(e.at))}] {e.trial:<14} {e.text}", e.style)
        for e in log.events
    ]
    return out[-height:]


def view_board(s: State, log: EventLog, height: int) -> list[Line]:
    out: list[Line] = [(f"{s.name} · {counts(s)}", "bold"), (s.eta, "cyan"), ("", "")]
    budget = f"/${s.budget:g}" if s.budget else ""
    out.append(
        (
            f"{'TRIAL':<15}{'STATE':<10}{'TIME':>7} {'TOOLS':>5} {'COST':>11}  {'PROGRESS':<34} NOW",
            "dim",
        )
    )
    for t in s.trials:
        quiet = s.now - (t.last_at or s.now)
        if t.state == "running":
            when = fmt(s.now - (t.started or s.now))
            pct = f"{t.progress:4.0%} " if t.progress is not None else "  ?  "
            prog = pct + t.basis
            now = t.last + (f"  (quiet {fmt(quiet)})" if quiet > 30 else "")
            if t.subagents:
                now = f"{len(t.subagents)} subagent(s) · " + now
        else:
            when = fmt(t.seconds) if t.seconds else ""
            prog = {"finished": "done", "failed": "failed", "queued": "waiting"}[t.state]
            now = t.reason
        cost = f"${t.cost:.2f}{budget}" if t.state != "queued" else ""
        tools = (
            f"{t.tools}" + (f"+{t.sub_tools}" if t.sub_tools else "") if t.state != "queued" else ""
        )
        out.append(
            (
                f"{t.id:<15}{t.state:<10}{when:>7} {tools:>5} {cost:>11}  {prog[:34]:<34} {now}",
                _style_for(t, s.now),
            )
        )
    return out


def view_bars(s: State, log: EventLog, height: int) -> list[Line]:
    total = len(s.trials)
    ended = sum(t.state in ("finished", "failed") for t in s.trials)
    partial = sum(t.progress or 0 for t in s.trials if t.state == "running")
    out: list[Line] = [
        (f"{'Experiment':<15}[{bar((ended + partial) / total, 30)}]  {counts(s)}", "bold"),
        (f"{'':<15}{s.eta}", "cyan"),
        ("", ""),
    ]
    for t in s.trials:
        st = _style_for(t, s.now)
        if t.state == "running":
            pct = f"{t.progress:4.0%}" if t.progress is not None else "  ? "
            out.append((f"{t.id:<15}[{bar(t.progress, 30)}] {pct}  {t.basis}", st))
            elapsed = s.now - (t.started or s.now)
            limits = []
            if s.timeout:
                limits.append(
                    f"time [{bar(elapsed / s.timeout, 10)}] {fmt(elapsed)}/{fmt(s.timeout)}"
                )
            else:
                limits.append(f"time {fmt(elapsed)}")
            if s.budget:
                limits.append(f"cost [{bar(t.cost / s.budget, 10)}] ${t.cost:.2f}/${s.budget:g}")
            out.append((f"{'':<15}" + "   ".join(limits), "dim"))
            quiet = s.now - (t.last_at or s.now)
            sub = f"{len(t.subagents)} subagent(s) running · " if t.subagents else ""
            out.append((f"{'':<15}now: {sub}{t.last.strip()} · {fmt(quiet)} ago", st or "dim"))
        elif t.state == "queued":
            out.append((f"{t.id:<15}[{bar(None, 30)}] waiting", st))
        else:
            tail = (
                f"done in {fmt(t.seconds)} · ${t.cost:.2f}" if t.state == "finished" else t.reason
            )
            out.append((f"{t.id:<15}[{bar(1.0 if t.state == 'finished' else 0, 30)}] {tail}", st))
    return out


def view_status(s: State, log: EventLog, height: int) -> list[Line]:
    running = [t for t in s.trials if t.state == "running"]
    foot: list[Line] = [("─" * 72, "dim")]
    spin = SPIN[int(time.time() * 8) % len(SPIN)] if running else "✓"
    foot.append((f"{spin} {counts(s)} · {s.eta}", "bold"))
    for t in running:
        quiet = s.now - (t.last_at or s.now)
        pct = f"{t.progress:.0%}" if t.progress is not None else "?"
        warn = f" · quiet {fmt(quiet)}!" if quiet > STALL else ""
        foot.append(
            (
                f"  {t.id:<14} {pct:>4} · {fmt(s.now - (t.started or s.now))} · {t.last.strip()}{warn}",
                _style_for(t, s.now),
            )
        )
    marks = {"green": "✓", "red": "✗", "yellow": "⚠", "bold": "▸"}
    body = [
        (f"{marks.get(e.style, '·')} {e.trial:<14} {e.text}", e.style)
        for e in log.events
        if e.milestone
    ]
    return body[-max(height - len(foot), 1) :] + foot


VARIANTS = {
    "A": ("Event log", view_log),
    "B": ("Board", view_board),
    "C": ("Bars", view_bars),
    "D": ("Log + status line", view_status),
}


def paint(lines: list[Line], width: int, color: bool) -> str:
    out = []
    for text, style in lines:
        text = text[: width - 1]
        out.append(f"\x1b[{STYLES[style]}m{text}\x1b[0m" if style and color else text)
    return "\x1b[H" + "\x1b[K\n".join(out) + "\x1b[K\x1b[J"


# --------------------------------------------------------------------------- simulator

TOOLS = [
    ("Bash", {"command": "git status --short", "description": "Check the working tree"}),
    ("Read", {"file_path": "src/calc.py"}),
    ("Grep", {"pattern": "def mul"}),
    ("Read", {"file_path": ".scratch/mul/PLAN.md"}),
    ("Edit", {"file_path": "src/calc.py"}),
    ("Edit", {"file_path": "tests/test_calc.py"}),
    ("Bash", {"command": "uv run pytest -q", "description": "Run the tests"}),
    ("Write", {"file_path": "src/parser.py"}),
    ("Bash", {"command": "ruff check .", "description": "Lint"}),
]
SCRIPTS = {  # trial id -> how it behaves
    "single-1": {"tools": 36, "todos": 6},
    "single-2": {"tools": 44},
    "single-3": {"tools": 14, "hang": True},
    "with-review-1": {"tools": 26, "plan": 8, "review": 12},
    "with-review-2": {"tools": 30, "plan": 8, "review": 10},
    "with-review-3": {"prebuild_fail": True},
}


class Simulator:
    """Writes the files a real run writes, on a sped-up clock."""

    def __init__(self, folder: Path, clock: Clock):
        self.folder, self.clock = folder, clock
        self.ws = folder / "_workspaces"
        self.msg = 0
        self.lock = threading.Lock()

    def sleep(self, lo: float, hi: float | None = None) -> None:
        time.sleep(random.uniform(lo, hi or lo) / self.clock.speed)

    def touch(self, path: Path) -> None:
        t = self.clock.now()
        os.utime(path, (t, t))

    def write_json(self, path: Path, data: dict) -> None:
        write_json(path, data)
        self.touch(path)

    def append(self, path: Path, text: str) -> None:
        with path.open("a") as f:
            f.write(text + "\n")
        self.touch(path)

    def start(self) -> None:
        self.write_json(
            self.folder / "experiment.json",
            {
                "name": "calc-mul (simulated)",
                "plan": ".scratch/mul",
                "workspaces": str(self.ws),
                "started_at": self.clock.now(),
                "settings": {
                    "concurrency": 2,
                    "limits": {"timeout_minutes": 20, "max_budget_usd": 5},
                    "variants": [
                        {"name": "single", "repeats": 3},
                        {"name": "with-review", "repeats": 3},
                    ],
                },
            },
        )
        threading.Thread(target=self._all, daemon=True).start()

    def _all(self) -> None:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(self._trial, SCRIPTS))

    def _event(self, stream: Path, block: dict, parent: str | None = None, out: int = 300) -> None:
        with self.lock:
            self.msg += 1
            n = self.msg
        usage = {
            "input_tokens": 5,
            "output_tokens": out,
            "cache_read_input_tokens": 20000 + n * 400,
            "cache_creation_input_tokens": 1500,
        }
        self.append(
            stream,
            json.dumps(
                {
                    "type": "assistant",
                    "parent_tool_use_id": parent,
                    "message": {
                        "id": f"msg_{n}",
                        "model": "claude-opus-5-5",
                        "usage": usage,
                        "content": [block],
                    },
                }
            ),
        )

    def _trial(self, tid: str) -> None:
        script = SCRIPTS[tid]
        tf = self.folder / "trials" / tid
        tf.mkdir(parents=True)
        variant = tid.rsplit("-", 1)[0]
        self.write_json(tf / "status.json", {"trial": tid, "variant": variant, "status": "running"})
        t0 = self.clock.now()
        self.sleep(6, 12)
        pre = tf / "pre_build.log"
        for step in ("uv sync", "uv run pytest -q"):
            self.append(pre, f"$ {step}")
            self.sleep(10, 30)
            if script.get("prebuild_fail") and step == "uv run pytest -q":
                self.append(pre, "1 failed, 12 passed")
                self.write_json(tf / "timings.json", {"pre_build_seconds": self.clock.now() - t0})
                self.write_json(
                    tf / "status.json",
                    {
                        "trial": tid,
                        "variant": variant,
                        "status": "failed",
                        "reason": "pre_build: `uv run pytest -q` exited with code 1",
                    },
                )
                return
        pre_s = self.clock.now() - t0
        stream = tf / "stream.jsonl"
        self.append(stream, json.dumps({"type": "system", "subtype": "init"}))
        a0 = self.clock.now()
        plan = self.ws / tid / "repo" / ".scratch/mul/PLAN.md"
        if script.get("plan"):
            plan.parent.mkdir(parents=True)
            plan.write_text("".join(f"- [ ] step {i + 1}\n" for i in range(script["plan"])))
        todos = [
            {"content": f"step {i + 1}", "status": "pending"} for i in range(script.get("todos", 0))
        ]
        if todos:
            self._event(
                stream, {"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}
            )
        total = script.get("tools", 0)
        for i in range(total):
            name, given = random.choice(TOOLS)
            self._event(
                stream, {"type": "tool_use", "id": f"tu_{tid}_{i}", "name": name, "input": given}
            )
            self.sleep(8, 28)
            if todos and i % max(total // len(todos), 1) == 0:
                k = sum(t["status"] == "completed" for t in todos)
                if k < len(todos):
                    todos[k]["status"] = "completed"
                    self._event(
                        stream, {"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}
                    )
            if script.get("plan") and i % max(total // script["plan"], 1) == 0:
                plan.write_text(plan.read_text().replace("- [ ]", "- [x]", 1))
            if script.get("hang") and i == total - 1:
                self.sleep(max(20 * 60 - (self.clock.now() - a0), 0))
                self.write_json(
                    tf / "timings.json", {"pre_build_seconds": pre_s, "agent_seconds": 1200}
                )
                self.write_json(
                    tf / "status.json",
                    {
                        "trial": tid,
                        "variant": variant,
                        "status": "failed",
                        "reason": "timeout: stopped after 20 minutes",
                    },
                )
                return
            if script.get("review") and i == total * 2 // 3:
                self._event(
                    stream,
                    {
                        "type": "tool_use",
                        "id": f"rv_{tid}",
                        "name": "Agent",
                        "input": {"description": "Review the change"},
                    },
                )
                self.append(
                    stream,
                    json.dumps(
                        {
                            "type": "system",
                            "subtype": "task_started",
                            "task_id": f"t_{tid}",
                            "description": "Review the change",
                        }
                    ),
                )
                for _ in range(script["review"]):
                    self._event(
                        stream,
                        {
                            "type": "tool_use",
                            "name": random.choice(TOOLS)[0],
                            "input": random.choice(TOOLS)[1],
                        },
                        parent=f"rv_{tid}",
                    )
                    self.sleep(6, 18)
                self.append(
                    stream,
                    json.dumps(
                        {
                            "type": "system",
                            "subtype": "task_notification",
                            "task_id": f"t_{tid}",
                            "status": "completed",
                        }
                    ),
                )
        if todos:
            for t in todos:
                t["status"] = "completed"
            self._event(
                stream, {"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}
            )
        self.append(stream, json.dumps({"type": "result", "subtype": "success"}))
        agent_s = self.clock.now() - a0
        self.sleep(4)
        self.write_json(tf / "timings.json", {"pre_build_seconds": pre_s, "agent_seconds": agent_s})
        self.write_json(
            tf / "status.json",
            {"trial": tid, "variant": variant, "status": "finished", "agent_seconds": agent_s},
        )


# --------------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(description="PROTOTYPE: live display for `build-bench run`")
    p.add_argument("--watch", metavar="RESULTS_FOLDER", help="watch a real run's results folder")
    p.add_argument("--speed", type=float, default=20, help="simulation speed-up (default 20)")
    p.add_argument("--variant", default="A", choices=list(VARIANTS))
    args = p.parse_args()

    if args.watch:
        folder, clock = Path(args.watch).resolve(), Clock(1)
    else:
        folder = Path(tempfile.mkdtemp(prefix="PROTOTYPE-progress-"))
        clock = Clock(args.speed)
        Simulator(folder, clock).start()
        time.sleep(0.2)

    keys = list(VARIANTS)
    current = args.variant
    log = EventLog()
    tty_in = sys.stdin.isatty() and sys.stdout.isatty()
    color = tty_in and not os.environ.get("NO_COLOR")
    old = termios.tcgetattr(sys.stdin) if tty_in else None
    if not tty_in:
        print("not a terminal: printing variant A (event log) only", file=sys.stderr)
    try:
        if tty_in:
            tty.setcbreak(sys.stdin)
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
        printed = 0
        while True:
            state = read_state(folder, clock.now())
            log.feed(state)
            if not tty_in:
                for e in log.events[printed:]:
                    print(
                        f"[{time.strftime('%H:%M:%S', time.localtime(e.at))}] {e.trial:<14} {e.text}"
                    )
                printed = len(log.events)
                if all(t.state in ("finished", "failed") for t in state.trials):
                    return
                time.sleep(0.5)
                continue
            width, height = shutil.get_terminal_size()
            name, view = VARIANTS[current]
            lines = view(state, log, height - 2)
            i = keys.index(current)
            switcher = (
                f"  ◀  {current} · {name}  ▶     ←/→ or 1-{len(keys)} to switch · q quit"
                f"   [PROTOTYPE{'' if args.watch else f', simulated {args.speed:g}x'}]"
            )
            filler = [("", "")] * max(height - 1 - len(lines), 0)
            sys.stdout.write(
                paint(
                    lines[: height - 1] + filler + [(switcher.ljust(width - 1), "inverse")],
                    width,
                    color,
                )
            )
            sys.stdout.flush()
            if select.select([sys.stdin], [], [], 0.25)[0]:
                key = os.read(sys.stdin.fileno(), 8).decode(errors="replace")
                if key in ("q", "Q", "\x1b"):
                    return
                if key in ("\x1b[C", "l"):
                    current = keys[(i + 1) % len(keys)]
                elif key in ("\x1b[D", "h"):
                    current = keys[(i - 1) % len(keys)]
                elif key.isdigit() and 1 <= int(key) <= len(keys):
                    current = keys[int(key) - 1]
    except KeyboardInterrupt:
        pass
    finally:
        if old is not None:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
