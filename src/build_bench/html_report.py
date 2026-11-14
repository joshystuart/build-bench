"""One self-contained HTML page: what went wrong, the summary tables and a timeline.

No network: styles and script are inline, the timeline is inline SVG. It has a
"Show as table" alternative, every agent's row takes keyboard focus and shows
its tooltip on focus, and the page follows the system light or dark theme with
a button to switch.

The markup is in `templates/report.html.j2`, with styles and script beside it in
`report.css` and `report.js`. This module works out what goes on the page,
including where every timeline bar goes. The template prints it, and autoescape
escapes every value.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from importlib.resources import files

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup, escape

from .report import failed_judging_lines, first_paragraph, flag_lines, tables

_TEMPLATES = files("build_bench") / "templates"

_ENV = Environment(
    loader=PackageLoader("build_bench"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_ENV.filters["px"] = lambda value: f"{value:.1f}"
_ENV.filters["minutes"] = lambda seconds: f"{seconds / 60:.2f}"


@dataclass
class Problem:
    trial: str
    what: str
    why: str
    quote: str | None  # The first paragraph of the agent's final message.
    whole: str | None  # The whole message, when it says more than the quote.


@dataclass
class Bar:
    kind: str  # main or sub
    x: float
    y: float
    w: float
    h: int
    rx: float


@dataclass
class Wait:
    x1: float
    x2: float
    y: float


@dataclass
class AgentRow:
    label: str
    tip: list[str]  # Tooltip lines, the headline value first.
    y: int
    height: int
    hit_x: float
    hit_w: float
    bars: list[Bar]
    waits: list[Wait] = field(default_factory=list)

    @property
    def text_y(self) -> float:
        return self.y + self.height / 2 + 4


@dataclass
class TrialGroup:
    trial: str
    note: str
    bad: bool
    y: int
    rows: list[AgentRow] = field(default_factory=list)


@dataclass
class Tick:
    x: float
    label: str
    axis: bool


@dataclass
class TimeRow:
    trial: str
    agent: str
    start: float
    end: float
    cost: float | None


@dataclass
class Timeline:
    top: int
    bottom: int
    right: int  # Where the bars end and the right-aligned notes are anchored.
    ticks: list[Tick]
    groups: list[TrialGroup]
    table: list[TimeRow]


def _problems(data: dict) -> list[Problem]:
    """Why each Trial went wrong: the recorded reason and the agent's own last words."""
    out = []
    for f in data["failed_trials"]:
        why = f"Failed: {f['reason'] or 'no reason was recorded'}"
        out.append(_problem(f["trial"], "failed", why, f["final_message"]))
    for f in data["empty_trials"]:
        why = "Finished but left no code changes. Its numbers are in the averages."
        out.append(_problem(f["trial"], "changed nothing", why, f["final_message"]))
    return out


def _problem(trial: str, what: str, why: str, words: str | None) -> Problem:
    if not words:
        return Problem(trial, what, why, None, None)
    quote = first_paragraph(words)
    whole = words if quote != " ".join(words.split()) else None
    return Problem(trial, what, why, quote, whole)


def _step(total_minutes: float) -> float:
    raw = max(total_minutes, 0.01) / 6
    power = 10 ** math.floor(math.log10(raw))
    return next(m * power for m in (1, 2, 5, 10) if m * power >= raw)


def _clock(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 60}m {s % 60:02d}s"


def _money(value: float | None) -> str:
    return "–" if value is None else f"${value:.2f}"


def _working(total: float, waits: list) -> list[tuple[float, float]]:
    """The stretches of [0, total] when the main agent was not waiting."""
    out, at = [], 0.0
    for start, end in sorted(waits):
        if start > at:
            out.append((at, start))
        at = max(at, end)
    if at < total:
        out.append((at, total))
    return out


def _timeline(rows: list[dict]) -> Timeline:
    """Every Trial's agents on one shared time scale, one group of rows per Trial."""
    scale = max(r["timeline"]["total_seconds"] for r in rows) or 1.0
    names = [s["name"] for r in rows for s in r["timeline"]["subagents"]] + ["Main agent"]
    # About 7 units a character at 12px, so the longest name ends before the bars start.
    left = min(280, max(110, 7 * max(len(n) for n in names) + 18))
    width, row_h, group_h, bar_h, top = 1000 - left - 12, 24, 30, 12, 8
    room = int((left - 18) / 7)

    def x(seconds: float) -> float:
        return left + width * seconds / scale

    def bar(kind: str, x0: float, x1: float, y: int) -> Bar:
        w = max(x1 - x0, 2.0)
        return Bar(kind, x0, y + (row_h - bar_h) / 2, w, bar_h, min(3.0, w / 2))

    def agent(name: str, tip: list[str], x0: float, x1: float, y: int, bars: list[Bar]):
        label = name if len(name) <= room else name[: room - 1] + "…"
        return AgentRow(label, tip, y, row_h, x0 - 4, max(x1 - x0 + 8, 24), bars)

    groups: list[TrialGroup] = []
    table: list[TimeRow] = []
    y = top
    for r in rows:
        t = r["timeline"]
        subs = t["subagents"]
        note = f"{_clock(t['total_seconds'])} · {len(subs)} subagent{'s' * (len(subs) != 1)}"
        bad = bool(r.get("changed_nothing"))
        if bad:
            note += " · changed nothing"
        group = TrialGroup(r["trial"], note, bad, y)
        groups.append(group)
        y += group_h

        total, waits = t["total_seconds"], t["waits"]
        waited = total - sum(e - s for s, e in _working(total, waits))
        tip = [f"{_clock(total)} in all", f"Main agent · {r['trial']}"]
        if subs:
            share = round(100 * waited / total) if total else 0
            tip.append(
                f"Working {_clock(total - waited)} · waiting on subagents "
                f"{_clock(waited)} ({share}%)"
            )
        tip.append(f"Cost {_money(t.get('main_cost_usd'))}")
        main = agent(
            "Main agent",
            tip,
            x(0),
            x(total),
            y,
            [bar("main", x(s), x(e), y) for s, e in _working(total, waits)],
        )
        main.waits = [
            Wait(x(s) + 2, max(x(e) - 2, x(s) + 2), y + row_h / 2)
            for s, e in waits
            if x(e) - x(s) >= 4
        ]
        group.rows.append(main)
        y += row_h
        table.append(TimeRow(r["trial"], "Main agent", 0.0, total, t.get("main_cost_usd")))

        for s in subs:
            tip = [
                _clock(s["end"] - s["start"]),
                f"{s['name']} · {r['trial']}",
                f"Ran from {_clock(s['start'])} to {_clock(s['end'])}",
                f"Cost {_money(s.get('cost_usd'))}",
            ]
            x0, x1 = x(s["start"]), x(s["end"])
            group.rows.append(agent(s["name"], tip, x0, x1, y, [bar("sub", x0, x1, y)]))
            y += row_h
            table.append(TimeRow(r["trial"], s["name"], s["start"], s["end"], s.get("cost_usd")))
        waiting = "Main agent waiting on subagents"
        table += [TimeRow(r["trial"], waiting, s, e, None) for s, e in waits]

    bottom = y + 4
    ticks = []
    step = _step(scale / 60)
    tick = 0.0
    while tick * 60 <= scale + 1e-9:
        ticks.append(Tick(x(tick * 60), f"{tick:g}", tick == 0))
        tick += step
    return Timeline(top, bottom, left + width, ticks, groups, table)


def _bullet(line: str) -> Markup:
    """One flag line, with its **bold** markdown as <strong>."""
    text = str(escape(line.removeprefix("- ")))
    return Markup(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text))


def html(data: dict) -> str:
    timed = [r for r in data["trials"] if r.get("timeline")]
    return _ENV.get_template("report.html.j2").render(
        experiment=data["experiment"],
        flags=[_bullet(line) for line in flag_lines(data)],
        problems=_problems(data),
        tables=tables(data),
        timeline=_timeline(timed) if timed else None,
        failed_judging=failed_judging_lines(data),
        css=Markup((_TEMPLATES / "report.css").read_text()),
        script=Markup((_TEMPLATES / "report.js").read_text()),
    )
