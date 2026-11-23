"""Self-contained HTML pages: report.html, diffs.html and insights.html.

report.html is what went wrong, the summary tables and a timeline. diffs.html
is the code each Trial produced (see `review.py`, which works out its data).
insights.html is a model's words (see `insights.py`) around charts drawn here
from the report's own numbers.

No network: styles and script are inline, the timeline is inline SVG. It has a
"Show as table" alternative, every agent's row takes keyboard focus and shows
its tooltip on focus, and the page follows the system light or dark theme with
a button to switch.

The markup is in `templates/report.html.j2` and `diffs.html.j2`, with styles and
scripts beside them. This module works out what goes on the page,
including where every timeline bar goes. The template prints it, and autoescape
escapes every value.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup, escape

from .report import (
    AGENT_NAMES,
    DIFFS,
    INSIGHTS,
    RETRY,
    categorised,
    failed_judging_lines,
    first_paragraph,
    flag_lines,
    tables,
)
from .results import read_json

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
        why = (
            "Finished but left no code changes, so it counts as not built: no Judge was "
            "asked about it, and only its cost and time are in the averages."
        )
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
        if r.get("idle_minutes"):
            note += f" · idle {_clock(r['idle_minutes'] * 60)}"
        if bad:
            note += " · changed nothing"
        group = TrialGroup(r["trial"], note, bad, y)
        groups.append(group)
        y += group_h

        total, waits, idle = t["total_seconds"], t["waits"], t.get("idle", [])
        waited = total - sum(e - s for s, e in _working(total, waits))
        idled = sum(e - s for s, e in idle)
        tip = [f"{_clock(total)} in all", f"Main agent · {r['trial']}"]
        if subs:
            share = round(100 * waited / total) if total else 0
            tip.append(
                f"Working {_clock(total - waited - idled)} · waiting on subagents "
                f"{_clock(waited)} ({share}%)"
            )
        if idle:
            tip.append(f"Idle, nothing running {_clock(idled)}")
        tip.append(f"Cost {_money(t.get('main_cost_usd'))}")
        main = agent(
            "Main agent",
            tip,
            x(0),
            x(total),
            y,
            # Idle waits are left blank: nothing was running.
            [bar("main", x(s), x(e), y) for s, e in _working(total, waits + idle)],
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
        table += [TimeRow(r["trial"], "Main agent idle", s, e, None) for s, e in idle]

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


def html(data: dict, insights: bool = False) -> str:
    timed = [r for r in data["trials"] if r.get("timeline")]
    return _ENV.get_template("report.html.j2").render(
        experiment=data["experiment"],
        insights=INSIGHTS if insights else None,
        flags=[_bullet(line) for line in flag_lines(data)],
        problems=_problems(data),
        tables=tables(data),
        timeline=_timeline(timed) if timed else None,
        failed_judging=failed_judging_lines(data),
        retry=RETRY.replace("`", ""),
        diffs=DIFFS,
        css=Markup((_TEMPLATES / "report.css").read_text()),
        script=Markup((_TEMPLATES / "report.js").read_text()),
    )


def embedded_json(data: object) -> Markup:
    """JSON that is safe inside a <script> element: no `</script>` or `<!--` can
    end it early, whatever a diff holds."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for char, code in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e")):
        text = text.replace(char, code)
    return Markup(text)


def view_link(trial: str, path: str | None = None, pin: int | None = None) -> str:
    """The diffs.html address of one Trial's diff of one file, or of one pin on it."""
    parts = [f"trial={quote(trial, safe='')}"]
    if path is not None:
        parts.append(f"file={quote(path, safe='/')}")
    if pin is not None:
        parts.append(f"pin={pin}")
    return "#" + "&".join(parts)


def _ignored_totals(data: dict) -> dict[str, dict[str, int]]:
    """Each Trial's lines added and removed over the files the ignore globs match."""
    totals: dict[str, dict[str, int]] = {}
    for row in data["rows"]:
        if row["ignored"]:
            for trial, c in row["cells"].items():
                total = totals.setdefault(trial, {"added": 0, "removed": 0, "files": 0})
                total["added"] += c["added"]
                total["removed"] += c["removed"]
                total["files"] += 1
    return totals


def diffs_html(data: dict) -> str:
    css = (_TEMPLATES / "report.css").read_text() + (_TEMPLATES / "diffs.css").read_text()
    return _ENV.get_template("diffs.html.j2").render(
        data=data,
        columns=[tid for v in data["variants"] for tid in v["trials"]],
        rows=[r for r in data["rows"] if not r["ignored"]],
        ignored=[r for r in data["rows"] if r["ignored"]],
        ignored_totals=_ignored_totals(data),
        pins=data["pins"],
        link=view_link,
        payload=embedded_json(data),
        css=Markup(css),
        script=Markup((_TEMPLATES / "diffs.js").read_text()),
    )


# --- insights.html ----------------------------------------------------------------

#: The heading over each part of insights.html when the writer gave none.
HEADINGS = {
    "inputs": "What was compared",
    "quality": "Defects per build",
    "cost": "Cost per build",
    "time": "Time per build",
    "runs": "Every run, minute by minute",
    "shared": "Defects every build has",
    "differing": "Defects that differ between builds",
    "comparisons": "Head to head",
}
#: How many roles get a colour of their own; any other subagent is grey.
ROLE_COLOURS = 5


@dataclass
class ChartRow:
    """One Variant's bar: its average, and a dot for each of its Trials."""

    name: str
    label: str  # The average, printed at the end of the row.
    width: float  # The bar's length, in % of the track.
    dots: list[float]  # Each Trial's value, in % of the track.
    tip: list[str]
    pick: bool
    waiting: float = 0.0  # The striped part of a time bar, in % of the track.


@dataclass
class Span:
    kind: str  # main, wait, idle, r1 … r5 or other
    x: float
    y: float
    w: float
    tip: str
    h: int = 14


@dataclass
class Run:
    label: str
    meta: list[str]
    aria: str
    height: int
    spans: list[Span]
    grid: list[float]


@dataclass
class RunGroup:
    variant: str
    summary: str
    pick: bool
    runs: list[Run]


@dataclass
class DefectRow:
    id: str
    title: str
    severity: str
    #: Per Variant, each built Trial's standing: present, disputed, absent or unchecked.
    cells: list[list[str]]


@dataclass
class Split:
    first: str
    second: str
    wins: tuple[int, int]
    same: int
    compared: int
    weight: str | None
    widths: tuple[float, float, float]  # first, same, second, in %


def _emphasis(text: str) -> Markup:
    """The writer's words, with *these words* made to stand out."""
    return Markup(re.sub(r"\*([^*]+)\*", r"<em>\1</em>", str(escape(text))))


def _nice(top: float) -> float:
    """The round number a chart's scale ends at, a little past `top`."""
    if top <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(top))
    return next(m * power for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if m * power >= top)


def _by_variant(data: dict, key: str, which: str) -> dict[str, list[float]]:
    rows = [
        r for r in data["trials"] if (r["built"] if which == "built" else r["status"] == "finished")
    ]
    found: dict[str, list[float]] = {}
    for r in rows:
        if r.get(key) is not None:
            found.setdefault(r["variant"], []).append(r[key])
    return found


def _chart(
    data: dict, values: dict[str, list[float]], means: dict[str, float], show, pick: str | None
) -> list[ChartRow]:
    """A bar per Variant with any value, on one scale, with a dot per Trial."""
    present = [v["name"] for v in data["variants"] if values.get(v["name"])]
    if not present:
        return []
    # The highest dot ends short of the track's end, clear of the value beside it.
    scale = _nice(max(max(values[n]) for n in present)) / 0.96
    rows = []
    for name in present:
        mine = values[name]
        tip = [show(means[name]), f"{name}, average of {len(mine)} build{'s' * (len(mine) != 1)}"]
        if len(mine) > 1:
            tip.append(f"Lowest {show(min(mine))} · highest {show(max(mine))}")
        rows.append(
            ChartRow(
                name,
                show(means[name]),
                100 * means[name] / scale,
                [100 * v / scale for v in mine],
                tip,
                name == pick,
            )
        )
    return rows


def _quality_chart(data: dict, pick: str | None) -> list[ChartRow]:
    if data["defects"] is None:
        return []
    key = "behaviour_defects" if categorised(data) else "defects_total"
    values = _by_variant(data, key, "built")
    means = {n: round(sum(v) / len(v), 2) for n, v in values.items() if v}
    return _chart(data, values, means, lambda v: f"{v:g}", pick)


def _cost_chart(data: dict, pick: str | None) -> list[ChartRow]:
    values = _by_variant(data, "cost_usd", "finished")
    means = {v["name"]: v["build"]["cost_usd"]["mean"] for v in data["variants"]}
    return _chart(data, values, means, lambda v: f"${v:.2f}", pick)


def _time_chart(data: dict, pick: str | None) -> list[ChartRow]:
    """Active minutes per build, the part spent waiting on subagents striped."""
    values = _by_variant(data, "active_minutes", "finished")
    means = {v["name"]: v["build"]["active_minutes"]["mean"] for v in data["variants"]}
    rows = _chart(data, values, means, lambda v: f"{v:.1f} min", pick)
    waits = {v["name"]: v["build"]["wait_minutes"]["mean"] or 0.0 for v in data["variants"]}
    for row in rows:
        active, wait = means[row.name], waits[row.name]
        row.waiting = row.width * wait / active if active else 0.0
        if wait:
            row.tip.append(f"Working {active - wait:.1f} min · waiting on subagents {wait:.1f} min")
    return rows


def _roles(saved: dict) -> tuple[list[str], dict[str, str]]:
    """The roles the writer named, and the colour class of each subagent name."""
    names: list[str] = []
    of: dict[str, str] = {}
    for role in saved["roles"][:ROLE_COLOURS]:
        if role["name"] and role["subagents"]:
            names.append(role["name"])
            for sub in role["subagents"]:
                of.setdefault(sub, f"r{len(names)}")
    return names, of


def _lanes(subs: list[dict]) -> list[int]:
    """The row each subagent goes in: the first whose last subagent has ended."""
    ends: list[float] = []
    lanes = []
    for s in subs:
        lane = next((i for i, end in enumerate(ends) if end + 3 <= s["start"]), len(ends))
        if lane == len(ends):
            ends.append(s["end"])
        else:
            ends[lane] = s["end"]
        lanes.append(lane)
    return lanes


#: How long, on the timelines, an idle stretch with nothing running is drawn.
IDLE_DRAWN = 20.0


def _squeezed(idle: list) -> Callable[[float], float]:
    """A Trial's clock with each idle stretch cut to `IDLE_DRAWN` seconds, so the
    timelines show the time the agents spent working."""
    stretches = sorted(idle)

    def at(t: float) -> float:
        cut = 0.0
        for s, e in stretches:
            if t <= s:
                break
            into = min(t, e) - s
            cut += into - IDLE_DRAWN * into / (e - s)
        return t - cut

    return at


def _runs(data: dict, role_of: dict[str, str], pick: str | None) -> tuple[list[RunGroup], float]:
    """Every Trial's agents on one time scale, idle stretches cut short, grouped by Variant."""
    timed = [r for r in data["trials"] if r.get("timeline")]
    if not timed:
        return [], 0.0
    clocks = {r["trial"]: _squeezed(r["timeline"].get("idle", [])) for r in timed}
    scale = max(clocks[r["trial"]](r["timeline"]["total_seconds"]) for r in timed) or 1.0
    grid = [1000 * s / scale for s in _ticks(scale, _step(scale / 60) * 60)[1:]]
    row, gap = 14, 4
    groups = []
    for v in data["variants"]:
        mine = sorted((r for r in timed if r["variant"] == v["name"]), key=lambda r: r["repeat"])
        if not mine:
            continue
        runs = []
        for r in mine:
            t = r["timeline"]
            total, waits, idle = t["total_seconds"], t["waits"], t.get("idle", [])
            subs = sorted(t["subagents"], key=lambda s: s["start"])
            idled = sum(e - s for s, e in idle)
            active = total - idled
            waited = total - sum(e - s for s, e in _working(total, waits))
            clock = clocks[r["trial"]]

            def x(seconds: float, clock=clock) -> float:
                return 1000 * clock(seconds) / scale

            def span(kind: str, s: float, e: float, y: float, tip: str, least=1.5, h=row):
                return Span(kind, x(s), y, max(x(e) - x(s), least), tip, h)

            main_tip = f"Main agent · {_clock(active)}"
            if subs:
                main_tip += f" · working {_clock(active - waited)}, waiting {_clock(waited)}"
            spans = [span("main", s, e, 0, main_tip) for s, e in _working(total, waits + idle)]
            spans += [span("wait", s, e, 0, main_tip) for s, e in waits]
            idle_tip = "Idle, nothing running · {}"
            spans += [span("idle", s, e, 6, idle_tip.format(_clock(e - s)), h=2) for s, e in idle]
            lanes = _lanes(subs)
            for sub, lane in zip(subs, lanes, strict=True):
                tip = f"{sub['name']} · ran {_clock(sub['end'] - sub['start'])}"
                y = row + 2 * gap + lane * (row + gap)
                spans.append(
                    span(role_of.get(sub["name"], "other"), sub["start"], sub["end"], y, tip, 3.0)
                )
            height = row + (2 * gap + (max(lanes) + 1) * (row + gap) - gap if lanes else 0)
            meta = [_clock(active)]
            if subs:
                share = round(100 * waited / active) if active else 0
                meta += [f"{len(subs)} subagent{'s' * (len(subs) != 1)}", f"{share}% waiting"]
            else:
                meta.append("no subagents")
            if idled:
                meta.append(f"idle {_clock(idled)}")
            aria = f"{r['trial']}: " + ", ".join(meta)
            runs.append(Run(f"Run {r['repeat']}", meta, aria, height, spans, grid))
        counts = [len(r["timeline"]["subagents"]) for r in mine]
        summary = f"{v['build']['active_minutes']['mean'] or 0:.1f} min on average · " + (
            f"{min(counts)}–{max(counts)} subagents"
            if min(counts) != max(counts)
            else f"{counts[0]} subagent{'s' * (counts[0] != 1)}"
        )
        groups.append(RunGroup(v["name"], summary, v["name"] == pick, runs))
    return groups, scale


def _ticks(scale: float, step: float) -> list[float]:
    out, at = [], 0.0
    while at <= scale + 1e-9:
        out.append(at)
        at += step
    return out


def _defect_rows(data: dict, defects: list[dict], titles: dict[str, str]) -> list[DefectRow]:
    built = {
        v["name"]: sorted(
            (r for r in data["trials"] if r["variant"] == v["name"] and r["built"]),
            key=lambda r: r["repeat"],
        )
        for v in data["variants"]
    }
    return [
        DefectRow(
            d["id"],
            titles.get(d["id"]) or d["title"],
            d["severity"],
            [[r["defects"].get(d["id"], "unchecked") for r in rows] for rows in built.values()],
        )
        for d in defects
    ]


def _splits(data: dict) -> list[Split]:
    out = []
    for c in data["comparisons"]:
        first, second = c["variants"]
        wins = (c["better"][first], c["better"][second])
        n = c["compared"] or 1
        out.append(
            Split(
                first,
                second,
                wins,
                c["same"],
                c["compared"],
                c["weight"],
                (100 * wins[0] / n, 100 * c["same"] / n, 100 * wins[1] / n),
            )
        )
    return out


def _quality_stats(data: dict) -> list[tuple[str, str]]:
    trials = data["trials"]
    stats = [(f"{sum(r['built'] for r in trials)}/{len(trials)}", "builds finished with changes")]
    ran = [r for r in trials if r["checks_passed"] is not None]
    if ran:
        failed = [
            f"{name} in {r['trial']}"
            for r in trials
            for name, result in r["checks"].items()
            if result in ("failed", "timed out")
        ]
        label = "passed every check"
        if failed:
            label += f" ({', '.join(failed[:2])}{' …' if len(failed) > 2 else ''} failed)"
        stats.append((f"{sum(bool(r['checks_passed']) for r in trials)}/{len(trials)}", label))
    measured = [d["measures"]["defects"] for d in data["differences"] if d["measures"]["defects"]]
    if measured and data["defects"] is not None:
        clear = sum(1 for m in measured if m["weight"] == "clear")
        stats.append((str(clear), f"of {len(measured)} differences in quality are clear"))
    return stats


def _setup(settings, folder: Path, data: dict, saved: dict) -> tuple[list, list]:
    """The inputs as label and value, and a card per Variant."""
    configured = {v.name: v for v in settings.variants}
    shown = [configured[v["name"]] for v in data["variants"] if v["name"] in configured]

    def model(v) -> str:
        return v.model + (f", {v.effort} effort" if v.effort else "")

    models = list(dict.fromkeys(model(v) for v in shown))
    agents = {v.agent for v in shown}
    facts = []
    if len(models) == 1:
        facts.append(("Model", models[0]))
    if saved["task"]:
        facts.append(("Task", saved["task"]))
    trials = [v["trials"] for v in data["variants"]]
    runs = f"{trials[0]} per Variant, " if len(set(trials)) == 1 else ""
    facts.append(("Runs", f"{runs}{sum(trials)} in all"))
    if data["judges"]:
        names = ", ".join(
            dict.fromkeys(j["model"].rsplit("/", 1)[-1] for j in data["judges"] if j["model"])
        )
        facts.append(("Judges", names))
    cards = []
    for v in data["variants"]:
        mine = configured.get(v["name"])
        meta = []
        if mine is not None and len(models) > 1:
            meta.append(model(mine))
        if mine is not None and len(agents) > 1:
            meta.append(AGENT_NAMES.get(mine.agent, mine.agent))
        skills = [p for p in (folder / "skills" / v["name"]).glob("*") if p.is_dir()]
        if skills:
            meta.append(f"{len(skills)} skill{'s' * (len(skills) != 1)}")
        cards.append((v["name"], saved["variants"].get(v["name"], ""), " · ".join(meta)))
    return facts, cards


def _waiting(data: dict) -> list[tuple[str, str]]:
    """The share of each Variant's active time its main agent spent waiting on subagents."""
    out = []
    for v in data["variants"]:
        active, wait = v["build"]["active_minutes"]["mean"], v["build"]["wait_minutes"]["mean"]
        if active and wait:
            out.append((f"{round(100 * wait / active)}%", v["name"]))
    return out


def insights_html(settings, folder: Path, data: dict, saved: dict) -> str:
    """insights.html: the writer's words around charts drawn from the report's numbers."""
    words = saved["words"]
    pick = words["pick"]
    experiment = read_json(folder / "experiment.json") or {}
    started = experiment.get("started_at")
    when = time.localtime(started) if started else None
    date = f"{when.tm_mday} {time.strftime('%b %Y', when)}" if when else ""
    role_names, role_of = _roles(words)
    groups, scale = _runs(data, role_of, pick)
    defects = [d for d in data["defects"] or [] if not d["in_base"]]
    titles = words["defect_titles"]
    used = {s.kind for g in groups for r in g.runs for s in r.spans}
    facts, cards = _setup(settings, folder, data, words)
    heading = {k: _emphasis(words["sections"].get(k) or v) for k, v in HEADINGS.items()}
    return _ENV.get_template("insights.html.j2").render(
        experiment=data["experiment"],
        eyebrow=" · ".join(p for p in ("Build experiment", data["experiment"], date) if p),
        headline=_emphasis(words["headline"]),
        stats=words["stats"],
        pick=pick,
        pick_title=words["pick_title"] or pick,
        facts=facts,
        cards=cards,
        heading=heading,
        quality=_quality_chart(data, pick),
        behaviour=categorised(data),
        quality_stats=_quality_stats(data),
        merge_failed=data["merge_failed"] is not None,
        judged=data["defects"] is not None,
        cost=_cost_chart(data, pick),
        time=_time_chart(data, pick),
        waiting=_waiting(data),
        groups=groups,
        minutes=f"{scale / 60:.0f}",
        axis=[(100 * s / scale, f"{s / 60:g} min") for s in _ticks(scale, _step(scale / 60) * 60)]
        if scale
        else [],
        roles=[(f"r{n}", name) for n, name in enumerate(role_names, 1) if f"r{n}" in used],
        idle="idle" in used,
        other="other" in used,
        variants=[v["name"] for v in data["variants"]],
        shared=_defect_rows(data, [d for d in defects if d["in_every_trial"]], titles),
        in_base=_defect_rows(data, [d for d in data["defects"] or [] if d["in_base"]], titles),
        differing=_defect_rows(data, [d for d in defects if not d["in_every_trial"]], titles),
        splits=_splits(data),
        caveats=words["caveats"],
        why=words["why"],
        next=words["next"],
        writer=saved.get("model"),
        cost_usd=saved.get("cost_usd"),
        css=Markup(
            (_TEMPLATES / "report.css").read_text() + (_TEMPLATES / "insights.css").read_text()
        ),
        script=Markup((_TEMPLATES / "report.js").read_text()),
    )
