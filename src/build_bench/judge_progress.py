"""What `judge` shows while it works: a line as each step starts, and in a
terminal a display redrawn below the printed lines.

Judging is steps (measure, find, merge, check, compare), each a list of asks:
one Judge about one Trial, a pair of Trials, or the Findings to merge. Each
step tells the board when an ask starts and ends; what a running Agent Judge
is doing, and what it has cost so far, is read from the stream it writes. An
API Judge writes nothing until it replies, so only its time is shown.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .progress import BAR, STALL_SECONDS, Line, Screen, Stream, _limit, bar, clock, read_stream
from .settings import Judge, Settings
from .transcript import default_prices

HOW_TO_STOP = "Ctrl+C stops judging; `build-bench judge` again carries on from what is saved."


@dataclass
class Ask:
    """One thing a step does: usually one Judge asked about one Trial."""

    label: str
    judge: Judge | None = None
    #: Where an Agent Judge writes its stream; None for an API Judge.
    stream: Path | None = None
    #: What a running ask is doing, when there is no stream to say.
    doing: str = "waiting for the reply"
    state: str = "waiting"  # waiting | running | ok | failed
    reason: str = ""
    started: float | None = None
    seconds: float | None = None


def ask_of(label: str, j: Judge, streams: Path) -> Ask:
    """An ask of Judge `j`, whose files are named after `streams` (as `_ask_agent` names them)."""
    if j.type == "api":
        return Ask(label, j)
    return Ask(label, j, streams.with_name(streams.name + ".stream.jsonl"), "starting the Judge")


class Board:
    """The steps of one `judge` and where each stands."""

    def __init__(self, settings: Settings, say: Callable[[str], None] = print):
        self.settings = settings
        self.say = say
        self.prices = {**default_prices(), **settings.prices}
        self.started = time.time()
        names = ["measure", "find", "merge", "check"]
        if any(j.compare is not None for j in settings.judges):
            names.append("compare")
        #: Each step's mark in the header: "" until it starts.
        self.marks = dict.fromkeys(names, "")
        self.name = ""
        self.asks: list[Ask] = []
        self._lock = threading.Lock()
        self._streams: dict[Path, tuple[tuple[int, int], Stream]] = {}

    @contextmanager
    def shown(self, live: bool) -> Iterator[None]:
        """With `live`, keep the display on screen; lines print above it."""
        if not live:
            yield
            return
        screen = Screen(self.frame)
        said, self.say = self.say, screen.say
        try:
            with screen:
                yield
                self.close()
        finally:
            self.say = said

    def step(self, name: str, asks: list[Ask], line: str) -> None:
        """Start step `name`, which has `asks` to do; `line` says what it will do."""
        with self._lock:
            self._end_step()
            self.name, self.asks = name, asks
            self.marks[name] = f"0/{len(asks)}" if asks else "✓"
        self.say(f"[judge] {name}: {line}")

    def close(self) -> None:
        with self._lock:
            self._end_step()
            self.name, self.asks = "", []

    def _end_step(self) -> None:
        if not self.name:
            return
        failed = sum(a.state == "failed" for a in self.asks)
        self.marks[self.name] = f"✗ {failed} failed" if failed else "✓"

    def asking(self, ask: Ask, work: Callable[[], dict]) -> dict:
        """Do one ask: `work` returns the saved record, whose status says how it ended."""
        ask.state, ask.started = "running", time.time()
        record: dict = {"status": "failed", "reason": "stopped"}
        try:
            record = work()
        finally:
            ask.seconds = time.time() - ask.started
            ok = record.get("status") == "ok"
            ask.reason = "" if ok else str(record.get("reason") or "")
            ask.state = "ok" if ok else "failed"
            with self._lock:
                if self.name and ask in self.asks:
                    done = sum(a.state in ("ok", "failed") for a in self.asks)
                    self.marks[self.name] = f"{done}/{len(self.asks)}"
        return record

    # --- The display ----------------------------------------------------------------

    def _stream(self, ask: Ask) -> tuple[Stream, float] | None:
        """What a running Agent Judge's stream says, and when it last grew. A stream
        older than the ask is from an earlier `judge`."""
        if ask.stream is None or not ask.stream.is_file():
            return None
        stat = ask.stream.stat()
        if stat.st_mtime < (ask.started or 0):
            return None
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._streams.get(ask.stream)
        if cached is None or cached[0] != key:
            cached = (key, read_stream(ask.stream, self.prices))
            self._streams[ask.stream] = cached
        return cached[1], stat.st_mtime

    def frame(self, now: float, height: int) -> list[Line]:
        with self._lock:
            name, asks, marks = self.name, list(self.asks), dict(self.marks)
        steps = " · ".join(f"{step} {mark}".strip() for step, mark in marks.items())
        pad = max([len(a.label) for a in asks] + [len("Judging")]) + 2
        head: list[Line] = [
            (f"{'Judging':<{pad}}{steps} · {clock(now - self.started)} elapsed", "bold")
        ]
        if not name:
            return head
        count = {s: sum(a.state == s for a in asks) for s in ("ok", "running", "failed")}
        ended = count["ok"] + count["failed"]
        head.append(
            (
                f"{name:<{pad}}[{bar(ended / len(asks) if asks else 1, BAR)}]  "
                f"{count['ok']}/{len(asks)} done · {count['running']} running · "
                f"{count['failed']} failed",
                "bold",
            )
        )
        if asks and ended < len(asks):
            head.append((" " * pad + self._time_left(asks, now), "cyan"))
            head.append((" " * pad + HOW_TO_STOP, "dim"))
        running: list[Line] = []
        for a in asks:
            if a.state == "running":
                running += self._running(a, now, pad)
        finished: list[Line] = [
            (f"{a.label:<{pad}}done in {clock(a.seconds)}", "green")
            for a in asks
            if a.state == "ok"
        ] + [(f"{a.label:<{pad}}failed: {a.reason}", "red") for a in asks if a.state == "failed"]
        waiting = [a.label for a in asks if a.state == "waiting"]
        tail: list[Line] = (
            [(" " * pad + "waiting: " + ", ".join(waiting), "dim")] if waiting else []
        )
        lines = head + running + finished + tail
        if len(lines) > height and finished:
            folded = [
                f"{word}: " + ", ".join(a.label for a in asks if a.state == state)
                for state, word in (("ok", "done"), ("failed", "failed"))
                if any(a.state == state for a in asks)
            ]
            lines = head + running + [(" " * pad + " · ".join(folded), "dim")] + tail
        if len(lines) > height:
            lines = lines[: max(height - 1, 1)] + [("…", "dim")]
        return lines

    def _running(self, a: Ask, now: float, pad: int) -> list[Line]:
        elapsed = now - (a.started or now)
        if a.judge is None:
            return [(f"{a.label:<{pad}}time {clock(elapsed)}   now: {a.doing}", "")]
        limits = self.settings.judge_limits(a.judge)
        timeout = limits.timeout_minutes and limits.timeout_minutes * 60
        shown = [_limit("time", elapsed, timeout, clock)]
        seen = self._stream(a)
        doing, last_at = a.doing, a.started or now
        if seen is not None:
            stream, last_at = seen
            shown.append(_limit("cost", stream.cost, limits.max_budget_usd, lambda d: f"${d:.2f}"))
            doing = stream.last or "waiting for the Judge's first step"
            if stream.ended:
                doing = "reading its reply"
            if stream.subagents:
                doing = f"{len(stream.subagents)} subagent(s) running · {doing}"
        quiet = now - last_at
        style = "yellow" if seen is not None and quiet >= STALL_SECONDS else ""
        if seen is None:
            when = ""
        elif style:
            when = f" · quiet for {clock(quiet)}"
        else:
            when = f" · {clock(quiet)} ago"
        kind = "api" if a.judge.type == "api" else "agent"
        return [
            (f"{a.label:<{pad}}{kind} {a.judge.model}   " + "   ".join(shown), style),
            (" " * pad + f"now: {doing}{when}", style or "dim"),
        ]

    def _time_left(self, asks: list[Ask], now: float) -> str:
        done = [a.seconds or 0 for a in asks if a.state == "ok"]
        if not done:
            return "time left in this step: unknown until an ask finishes"
        typical = statistics.median(done)
        seconds = sum(
            typical if a.state == "waiting" else max(typical - (now - (a.started or now)), 0)
            for a in asks
            if a.state in ("waiting", "running")
        )
        concurrency = 1 if self.name in ("measure", "merge") else self.settings.concurrency
        return (
            f"time left in this step: ~{clock(seconds / concurrency)} "
            f"(from {len(done)} finished ask(s))"
        )
