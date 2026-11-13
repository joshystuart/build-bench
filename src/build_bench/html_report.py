"""One self-contained HTML page: what went wrong, the summary tables and a timeline.

No network: styles and script are inline, the timeline is inline SVG. It has a
"Show as table" alternative, every agent's row takes keyboard focus and shows
its tooltip on focus, and the page follows the system light or dark theme with
a button to switch.
"""

from __future__ import annotations

import math
import re
from html import escape

from .report import BUILD, OUTPUT, SCORE_COLUMNS, cell, first_paragraph, flag_lines, num

_DARK = """--page: #0d0d0d; --surface: #1a1a19; --fg: #f0efec; --muted: #c3c2b7;
  --faint: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255, 255, 255, 0.10);
  --main: #3987e5; --sub: #d95926; --bad: #e5484d; --shadow: 0 6px 20px rgba(0, 0, 0, 0.45);"""

CSS = (
    """
:root { color-scheme: light; --page: #f9f9f7; --surface: #fcfcfb; --fg: #0b0b0b;
  --muted: #52514e; --faint: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
  --border: rgba(11, 11, 11, 0.10); --main: #2a78d6; --sub: #eb6834;
  --bad: #c62a2f; --shadow: 0 6px 20px rgba(11, 11, 11, 0.10); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark; """
    + _DARK
    + """ } }
:root[data-theme="dark"] { color-scheme: dark; """
    + _DARK
    + """ }
* { box-sizing: border-box; }
html, body { background: var(--page); }
body { margin: 0; color: var(--fg);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; }
main { max-width: 1100px; margin: 0 auto; padding: 32px 16px 72px; }
h1 { font-size: clamp(26px, 4vw, 36px); line-height: 1.15; font-weight: 650;
  letter-spacing: -0.01em; margin: 0.3em 0 0.5em; }
h2 { font-size: 20px; font-weight: 650; margin: 48px 0 10px; }
.muted { color: var(--muted); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-variant-numeric: tabular-nums; margin: 0.5em 0;
  font-size: 14px; }
th, td { border-bottom: 1px solid var(--grid); padding: 6px 10px; text-align: right;
  white-space: nowrap; }
thead th { white-space: normal; vertical-align: bottom; min-width: 4.5em;
  color: var(--muted); font-size: 13px; font-weight: 600; }
th:first-child, td:first-child { text-align: left; }
tbody th { font-weight: 600; }
details { margin: 0.4em 0 1em; }
summary { cursor: pointer; color: var(--muted); font-size: 13px; }
summary:hover { color: var(--fg); }
.problems { list-style: none; padding: 0; margin: 0; }
.problems > li { background: var(--surface); border: 1px solid var(--border);
  border-left: 3px solid var(--bad); border-radius: 10px; padding: 12px 16px; margin: 0 0 10px; }
.problems .why { margin: 4px 0 0; }
.problems blockquote { margin: 6px 0 0; color: var(--muted); }
.problems details { margin: 6px 0 0; }
.problems pre { white-space: pre-wrap; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo,
  monospace; color: var(--muted); margin: 6px 0 0; }
figure.chart { margin: 16px 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; padding: 18px 18px 12px; }
.chart-title { font-size: 15px; font-weight: 650; margin: 0; }
.chart-sub { font-size: 13px; color: var(--muted); margin: 2px 0 12px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--muted);
  margin: 0 0 10px; padding: 0; list-style: none; }
.legend li { display: flex; align-items: center; gap: 7px; }
.sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.sw.main { background: var(--main); }
.sw.sub { background: var(--sub); }
.lk { width: 16px; height: 2px; border-radius: 1px; display: inline-block;
  background: var(--faint); }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg.timeline { min-width: 720px; }
svg text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.tick { font-size: 11px; fill: var(--faint); font-variant-numeric: tabular-nums; }
.row-name { font-size: 12px; fill: var(--muted); }
.group-label { font-size: 13px; font-weight: 650; fill: var(--fg); }
.group-note { font-size: 12px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.group-note.bad { fill: var(--bad); font-weight: 600; }
.grid { stroke: var(--grid); stroke-width: 1; shape-rendering: crispEdges; }
.axis { stroke: var(--axis); stroke-width: 1; shape-rendering: crispEdges; }
.mark { transition: opacity 0.12s ease; }
.mark.main { fill: var(--main); }
.mark.sub { fill: var(--sub); }
.wait-track { stroke: var(--faint); stroke-width: 2; stroke-linecap: round; }
.agent { outline: none; cursor: default; }
.agent .hit { fill: transparent; pointer-events: all; }
.agent:hover .mark, .agent:focus .mark { opacity: 0.72; }
.agent:hover .wait-track, .agent:focus .wait-track { stroke: var(--muted); }
.agent:focus-visible .hit { stroke: var(--fg); stroke-width: 1.5; }
#tip { position: fixed; z-index: 20; pointer-events: none; max-width: min(320px, 90vw);
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 8px 11px; box-shadow: var(--shadow); font-size: 13px; line-height: 1.45; }
#tip[hidden] { display: none; }
.tip-value { font-size: 15px; font-weight: 650; }
.tip-label { color: var(--muted); }
.theme { float: right; font: inherit; font-size: 13px; color: var(--muted);
  background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
  padding: 4px 12px; cursor: pointer; }
.theme:hover { color: var(--fg); }
@media (max-width: 600px) { body { font-size: 14px; } th, td { padding: 4px 6px; } }
"""
)

SCRIPT = """
(function () {
  var tip = document.getElementById('tip');
  function fill(el) {
    tip.replaceChildren();
    el.getAttribute('data-tip').split('\\n').forEach(function (line, i) {
      var row = document.createElement('div');
      row.className = i === 0 ? 'tip-value' : 'tip-label';
      row.textContent = line;
      tip.appendChild(row);
    });
    tip.hidden = false;
  }
  function place(x, y) {
    var pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    var left = x + pad, top = y + pad;
    if (left + w > window.innerWidth - 8) left = x - w - pad;
    if (top + h > window.innerHeight - 8) top = y - h - pad;
    tip.style.left = Math.max(8, left) + 'px';
    tip.style.top = Math.max(8, top) + 'px';
  }
  function hide() { tip.hidden = true; }
  document.querySelectorAll('.agent').forEach(function (el) {
    el.addEventListener('pointerenter', function (e) { fill(el); place(e.clientX, e.clientY); });
    el.addEventListener('pointermove', function (e) { place(e.clientX, e.clientY); });
    el.addEventListener('pointerleave', hide);
    el.addEventListener('focus', function () {
      fill(el);
      var r = el.getBoundingClientRect();
      place(r.left + Math.min(r.width / 2, 120), r.bottom - 6);
    });
    el.addEventListener('blur', hide);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
  var button = document.getElementById('theme');
  button.addEventListener('click', function () {
    var root = document.documentElement;
    var dark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') &&
       window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
})();
"""


def _summary_table(variants: list[dict], group: str, columns, judge: str | None = None) -> str:
    head = "".join(f"<th scope='col'>{escape(label)}</th>" for _, label, _ in columns)
    rows = []
    for v in variants:
        values = v[group][judge] if judge else v[group]
        cells = "".join(f"<td>{escape(cell(values[key]))}</td>" for key, _, _ in columns)
        rows.append(
            f"<tr><th scope='row'>{escape(v['name'])}</th>"
            f"<td>{v['finished']}/{v['trials']}</td>{cells}</tr>"
        )
    return (
        "<div class='scroll'><table><thead><tr><th scope='col'>Variant</th>"
        f"<th scope='col'>Finished</th>{head}</tr></thead><tbody>{''.join(rows)}"
        "</tbody></table></div>"
    )


def _problems(data: dict) -> str:
    """Why each Trial went wrong: the recorded reason and the agent's own last words."""
    if not data["failed_trials"] and not data["empty_trials"]:
        return ""
    items = []
    for f in data["failed_trials"]:
        why = f"<p class='why'>Failed: {escape(str(f['reason'] or 'no reason was recorded'))}</p>"
        items.append(_problem(f["trial"], "failed", why, f["final_message"]))
    for f in data["empty_trials"]:
        why = (
            "<p class='why'>Finished but left no code changes. Its numbers are in the averages.</p>"
        )
        items.append(_problem(f["trial"], "changed nothing", why, f["final_message"]))
    return "<h2>What went wrong</h2><ul class='problems'>" + "".join(items) + "</ul>"


def _problem(trial: str, what: str, why: str, words: str | None) -> str:
    said = "<p class='why muted'>The agent wrote no final message.</p>"
    if words:
        said = f"<blockquote>“{escape(first_paragraph(words))}”</blockquote>"
        if first_paragraph(words) != " ".join(words.split()):
            said += (
                "<details><summary>The agent's whole final message</summary>"
                f"<pre>{escape(words)}</pre></details>"
            )
    head = f"<strong>{escape(trial)}</strong> <span class='muted'>· {what}</span>"
    return f"<li>{head}{why}{said}</li>"


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


def _timelines(rows: list[dict]) -> str:
    """Every Trial's agents on one shared time scale, one group of rows per Trial."""
    scale = max(r["timeline"]["total_seconds"] for r in rows) or 1.0
    names = [s["name"] for r in rows for s in r["timeline"]["subagents"]] + ["Main agent"]
    # About 7 units a character at 12px, so the longest name ends before the bars start.
    left = min(280, max(110, 7 * max(len(n) for n in names) + 18))
    width, row_h, group_h, bar_h, top = 1000 - left - 12, 24, 30, 12, 8

    def x(seconds: float) -> float:
        return left + width * seconds / scale

    body: list[str] = []
    table: list[str] = []
    y = top
    for r in rows:
        t = r["timeline"]
        trial = escape(r["trial"])
        subs = t["subagents"]
        note = f"{_clock(t['total_seconds'])} · {len(subs)} subagent{'s' * (len(subs) != 1)}"
        bad = ""
        if r["status"] != "finished":
            note, bad = note + " · failed", " bad"
        elif r.get("changed_nothing"):
            note, bad = note + " · changed nothing", " bad"
        body.append(
            f"<g class='trial' role='group' aria-label='Timeline of {trial}'>"
            f"<text class='group-label' x='0' y='{y + 20}'>{trial}</text>"
            f"<text class='group-note{bad}' x='{left + width}' y='{y + 20}' "
            f"text-anchor='end'>{escape(note)}</text>"
        )
        y += group_h

        total, waits = t["total_seconds"], t["waits"]
        waited = sum(e - s for s, e in waits)
        tip = [f"{_clock(total)} in all", f"Main agent · {r['trial']}"]
        if subs:
            share = round(100 * waited / total) if total else 0
            tip.append(
                f"Working {_clock(total - waited)} · waiting on subagents "
                f"{_clock(waited)} ({share}%)"
            )
        tip.append(f"Cost {_money(t.get('main_cost_usd'))}")
        marks = [
            f"<line class='wait-track' x1='{x(s) + 2:.1f}' x2='{max(x(e) - 2, x(s) + 2):.1f}' "
            f"y1='{y + row_h / 2:.1f}' y2='{y + row_h / 2:.1f}'/>"
            for s, e in waits
            if x(e) - x(s) >= 4
        ]
        marks += [_bar(x(s), x(e), y, row_h, bar_h, "main") for s, e in _working(total, waits)]
        body.append(_agent_row("Main agent", tip, left, x(0) - 4, x(total) + 4, y, row_h, marks))
        y += row_h
        table.append(_table_row(r["trial"], "Main agent", 0.0, total, t.get("main_cost_usd")))

        for s in subs:
            tip = [
                _clock(s["end"] - s["start"]),
                f"{s['name']} · {r['trial']}",
                f"Ran from {_clock(s['start'])} to {_clock(s['end'])}",
                f"Cost {_money(s.get('cost_usd'))}",
            ]
            x0, x1 = x(s["start"]), x(s["end"])
            mark = _bar(x0, x1, y, row_h, bar_h, "sub")
            body.append(_agent_row(s["name"], tip, left, x0 - 4, x1 + 4, y, row_h, [mark]))
            y += row_h
            table.append(_table_row(r["trial"], s["name"], s["start"], s["end"], s.get("cost_usd")))
        waiting = "Main agent waiting on subagents"
        table += [_table_row(r["trial"], waiting, s, e, None) for s, e in waits]
        body.append("</g>")

    bottom = y + 4
    grid = []
    step = _step(scale / 60)
    tick = 0.0
    while tick * 60 <= scale + 1e-9:
        tx = x(tick * 60)
        grid.append(
            f"<line class='{'axis' if tick == 0 else 'grid'}' x1='{tx:.1f}' x2='{tx:.1f}' "
            f"y1='{top}' y2='{bottom}'/><text class='tick' x='{tx:.1f}' y='{bottom + 16}' "
            f"text-anchor='middle'>{tick:g}</text>"
        )
        tick += step
    grid.append(
        f"<text class='tick' x='{left + width}' y='{bottom + 34}' text-anchor='end'>minutes</text>"
    )
    svg = (
        f"<div class='scroll'><svg class='timeline' viewBox='0 0 1000 {bottom + 40}' "
        "role='group' aria-label='Timeline of every agent in every Trial'>"
        + "".join(grid)
        + "".join(body)
        + "</svg></div>"
    )
    return (
        "<figure class='chart'><p class='chart-title'>Which agent was running, minute by "
        "minute</p><p class='chart-sub'>Each bar is one agent, from its first message to its "
        "last. Every Trial uses the same time scale. Hover or tab to a row to read it.</p>"
        "<ul class='legend' aria-label='Legend'><li><span class='sw main'></span>Main agent "
        "working</li><li><span class='lk'></span>Main agent waiting on subagents</li>"
        "<li><span class='sw sub'></span>Subagent</li></ul>"
        + svg
        + "<details><summary>Show as table</summary><div class='scroll'><table><thead><tr>"
        "<th scope='col'>Trial</th><th scope='col'>Agent</th><th scope='col'>Start (min)</th>"
        "<th scope='col'>End (min)</th><th scope='col'>Length (min)</th>"
        f"<th scope='col'>Cost ($)</th></tr></thead><tbody>{''.join(table)}</tbody></table>"
        "</div></details></figure>"
    )


def _bar(x0: float, x1: float, y: float, row_h: int, bar_h: int, kind: str) -> str:
    w = max(x1 - x0, 2.0)
    return (
        f"<rect class='mark {kind}' x='{x0:.1f}' y='{y + (row_h - bar_h) / 2:.1f}' "
        f"width='{w:.1f}' height='{bar_h}' rx='{min(3.0, w / 2):.1f}'/>"
    )


def _agent_row(name, tip, left, x0, x1, y, row_h, marks) -> str:
    room = int((left - 18) / 7)
    label = name if len(name) <= room else name[: room - 1] + "…"
    lines = escape("\n".join(tip))
    return (
        f"<g class='agent' tabindex='0' role='img' aria-label='{escape(', '.join(tip))}' "
        f"data-tip='{lines}'><text class='row-name' x='0' y='{y + row_h / 2 + 4:.1f}'>"
        f"{escape(label)}</text><rect class='hit' x='{x0:.1f}' y='{y}' "
        f"width='{max(x1 - x0, 24):.1f}' height='{row_h}'/>{''.join(marks)}</g>"
    )


def _table_row(trial: str, agent: str, start: float, end: float, cost: float | None) -> str:
    return (
        f"<tr><td>{escape(trial)}</td><th scope='row'>{escape(agent)}</th>"
        f"<td>{start / 60:.2f}</td><td>{end / 60:.2f}</td><td>{(end - start) / 60:.2f}</td>"
        f"<td>{'–' if cost is None else f'{cost:.4f}'}</td></tr>"
    )


def _bullets(lines: list[str]) -> str:
    items = []
    for line in lines:
        text = escape(line.removeprefix("- "))
        items.append("<li>" + re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text) + "</li>")
    return "<ul>" + "".join(items) + "</ul>"


def html(data: dict) -> str:
    body = [
        "<button class='theme' id='theme' type='button'>Switch theme</button>",
        f"<h1>{escape(data['experiment'])}</h1>",
        _bullets(flag_lines(data)),
        _problems(data),
        "<h2>Build</h2><p class='muted'>Each cell is the average over finished Trials, with "
        "the lowest and highest in brackets. Failed Trials are left out.</p>",
        _summary_table(data["variants"], "build", BUILD),
        "<h2>Output</h2>",
        _summary_table(data["variants"], "output", OUTPUT),
    ]
    for j in data["judges"]:
        body.append(f"<h2>Scores: {escape(j['name'])}</h2>")
        body.append(_summary_table(data["variants"], "scores", SCORE_COLUMNS, judge=j["name"]))
    if data["judges"]:
        rows = "".join(
            f"<tr><th scope='row'>{escape(j['name'])}</th><td>{escape(j['type'] or '–')}</td>"
            f"<td>{escape(j['model'] or '–')}</td><td>{j['verdicts']}</td><td>{j['failed']}</td>"
            f"<td>{num(j['cost_usd'])}</td><td>{num(j['minutes'])}</td></tr>"
            for j in data["judges"]
        )
        body.append(
            "<h2>Judges</h2><p class='muted'>Judging cost and time, kept apart from the "
            "build numbers.</p><div class='scroll'><table><thead><tr><th scope='col'>Judge</th>"
            "<th scope='col'>Type</th><th scope='col'>Model</th><th scope='col'>Verdicts</th>"
            "<th scope='col'>Failed</th><th scope='col'>Cost ($)</th>"
            f"<th scope='col'>Time (min)</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    timed = [r for r in data["trials"] if r.get("timeline")]
    if timed:
        body.append("<h2>Timelines</h2>")
        body.append(_timelines(timed))
    if data["failed_verdicts"]:
        body.append("<h2>Failed Verdicts</h2><ul>")
        body += [
            f"<li>{escape(f['trial'])} by {escape(f['judge'])}: {escape(str(f['reason']))}</li>"
            for f in data["failed_verdicts"]
        ]
        body.append("</ul>")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(data['experiment'])} report</title><style>{CSS}</style></head>"
        f"<body><main>{''.join(body)}</main><div id='tip' role='tooltip' hidden></div>"
        f"<script>{SCRIPT}</script></body></html>\n"
    )
