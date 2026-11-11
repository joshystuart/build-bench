"""One self-contained HTML page: the summary tables and a timeline per Trial.

No network: styles and script are inline, charts are inline SVG. Every chart
has a "Show as table" alternative, every bar takes keyboard focus and shows
its tooltip on focus, and the page follows the system light or dark theme
with a button to switch.
"""

from __future__ import annotations

import math
from html import escape

from .report import BUILD, OUTPUT, SCORE_COLUMNS, cell, flag_lines, num

CSS = """
:root { color-scheme: light; --bg: #ffffff; --fg: #1b1f24; --muted: #59636e;
  --line: #d1d9e0; --panel: #f6f8fa; --main: #2f6fbf; --sub: #2e9e6b;
  --wait: #c9822b; --focus: #8250df; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark; --bg: #0f1216; --fg: #e6edf3; --muted: #9aa5b1;
  --line: #30363d; --panel: #161b22; --main: #5b9be6; --sub: #4cc38a;
  --wait: #e3a456; --focus: #b392f0; } }
:root[data-theme="dark"] { color-scheme: dark; --bg: #0f1216; --fg: #e6edf3;
  --muted: #9aa5b1; --line: #30363d; --panel: #161b22; --main: #5b9be6;
  --sub: #4cc38a; --wait: #e3a456; --focus: #b392f0; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.6rem; margin: 0.4em 0; }
h2 { font-size: 1.2rem; margin-top: 1.8em; border-bottom: 1px solid var(--line);
  padding-bottom: 0.2em; }
h3 { font-size: 1rem; margin: 1.2em 0 0.4em; }
p, li { color: var(--fg); }
.muted { color: var(--muted); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-variant-numeric: tabular-nums; margin: 0.5em 0; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: right;
  white-space: nowrap; }
thead th { white-space: normal; vertical-align: bottom; min-width: 4.5em; }
th:first-child, td:first-child { text-align: left; }
th { background: var(--panel); }
details { margin: 0.4em 0 1em; }
summary { cursor: pointer; color: var(--muted); }
svg { width: 100%; height: auto; display: block; }
svg.timeline { min-width: 800px; }
svg text { fill: var(--fg); font-size: 12px; }
svg .axis { stroke: var(--line); }
svg .tick { fill: var(--muted); }
.bar { cursor: default; }
.bar:focus { outline: none; }
.bar:focus rect, .bar:hover rect { stroke: var(--focus); stroke-width: 2; }
.main rect { fill: var(--main); }
.sub rect { fill: var(--sub); }
.wait rect { fill: var(--wait); }
.legend span { display: inline-block; margin-right: 1em; }
.swatch { display: inline-block; width: 0.9em; height: 0.9em; vertical-align: -0.1em;
  margin-right: 0.3em; border-radius: 2px; }
#tip { position: fixed; pointer-events: none; background: var(--panel); color: var(--fg);
  border: 1px solid var(--line); border-radius: 4px; padding: 4px 8px; font-size: 13px;
  max-width: min(320px, 90vw); display: none; z-index: 10; }
.theme { float: right; background: var(--panel); color: var(--fg);
  border: 1px solid var(--line); border-radius: 4px; padding: 4px 10px; cursor: pointer; }
@media (max-width: 600px) { body { font-size: 14px; } th, td { padding: 3px 5px; } }
"""

SCRIPT = """
(function () {
  var tip = document.getElementById('tip');
  function show(el) {
    tip.textContent = el.getAttribute('aria-label');
    tip.style.display = 'block';
    var r = el.getBoundingClientRect();
    var x = Math.min(r.left, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = Math.max(8, x) + 'px';
    tip.style.top = (r.top - tip.offsetHeight - 6 < 0 ? r.bottom + 6
                     : r.top - tip.offsetHeight - 6) + 'px';
  }
  function hide() { tip.style.display = 'none'; }
  document.querySelectorAll('.bar').forEach(function (el) {
    el.addEventListener('focus', function () { show(el); });
    el.addEventListener('mouseenter', function () { show(el); });
    el.addEventListener('blur', hide);
    el.addEventListener('mouseleave', hide);
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


def _step(total_minutes: float) -> float:
    raw = max(total_minutes, 0.01) / 6
    power = 10 ** math.floor(math.log10(raw))
    return next(m * power for m in (1, 2, 5, 10) if m * power >= raw)


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def _timeline(row: dict, scale_seconds: float) -> str:
    t = row["timeline"]
    left, width, row_h = 190, 790, 24
    bars = [("Main agent", 0.0, t["total_seconds"], "main")]
    bars += [(s["name"], s["start"], s["end"], "sub") for s in t["subagents"]]
    height = row_h * len(bars) + 34

    def x(seconds: float) -> float:
        return left + width * (seconds / scale_seconds if scale_seconds else 0)

    parts = [
        f"<div class='scroll'><svg class='timeline' viewBox='0 0 1000 {height}' role='group' "
        f"aria-label='Timeline of {escape(row['trial'])}'>"
    ]
    for i, (name, start, end, kind) in enumerate(bars):
        y = 6 + i * row_h
        label = name if len(name) <= 22 else name[:21] + "…"
        parts.append(f"<text x='{left - 8}' y='{y + 14}' text-anchor='end'>{escape(label)}</text>")
        tip = f"{name}: {_minutes(start)} to {_minutes(end)} ({_minutes(end - start)})"
        parts.append(
            f"<g class='bar {kind}' tabindex='0' aria-label='{escape(tip)}'>"
            f"<rect x='{x(start):.1f}' y='{y}' width='{max(x(end) - x(start), 1.5):.1f}' "
            f"height='{row_h - 8}' rx='2'/></g>"
        )
        if kind == "main":
            for w_start, w_end in t["waits"]:
                wait_tip = (
                    f"Main agent waiting on subagents: {_minutes(w_start)} to "
                    f"{_minutes(w_end)} ({_minutes(w_end - w_start)})"
                )
                parts.append(
                    f"<g class='bar wait' tabindex='0' aria-label='{escape(wait_tip)}'>"
                    f"<rect x='{x(w_start):.1f}' y='{y + 4}' "
                    f"width='{max(x(w_end) - x(w_start), 1.5):.1f}' height='{row_h - 16}'/>"
                    "</g>"
                )
    axis_y = 6 + len(bars) * row_h
    parts.append(
        f"<line class='axis' x1='{left}' x2='{left + width}' y1='{axis_y}' y2='{axis_y}'/>"
    )
    step = _step(scale_seconds / 60)
    tick = 0.0
    while tick * 60 <= scale_seconds + 1e-9:
        tx = x(tick * 60)
        parts.append(
            f"<line class='axis' x1='{tx:.1f}' x2='{tx:.1f}' y1='{axis_y}' y2='{axis_y + 4}'/>"
            f"<text class='tick' x='{tx:.1f}' y='{axis_y + 18}' text-anchor='middle'>"
            f"{tick:g}</text>"
        )
        tick += step
    parts.append(
        f"<text class='tick' x='{left - 8}' y='{axis_y + 18}' text-anchor='end'>minutes</text>"
    )
    parts.append("</svg></div>")

    rows = [
        f"<tr><th scope='row'>{escape(name)}</th><td>{start / 60:.2f}</td>"
        f"<td>{end / 60:.2f}</td><td>{(end - start) / 60:.2f}</td></tr>"
        for name, start, end, _ in bars
    ]
    rows += [
        f"<tr><th scope='row'>Waiting on subagents</th><td>{s / 60:.2f}</td>"
        f"<td>{e / 60:.2f}</td><td>{(e - s) / 60:.2f}</td></tr>"
        for s, e in t["waits"]
    ]
    table = (
        "<details><summary>Show as table</summary><div class='scroll'><table><thead><tr>"
        "<th scope='col'>Agent</th><th scope='col'>Start (min)</th>"
        "<th scope='col'>End (min)</th><th scope='col'>Length (min)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></details>"
    )
    return "".join(parts) + table


def _bullets(lines: list[str]) -> str:
    import re

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
        "<p class='muted'>Each cell is the average over finished Trials, with the lowest "
        "and highest in brackets. Failed Trials are left out.</p>",
        "<h2>Build</h2>",
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
        scale = max(r["timeline"]["total_seconds"] for r in timed)
        body.append(
            "<h2>Timelines</h2><p class='muted'>Every timeline uses the same time scale. "
            "Tab to a bar to read it.</p><p class='legend'>"
            "<span><span class='swatch' style='background:var(--main)'></span>Main agent</span>"
            "<span><span class='swatch' style='background:var(--sub)'></span>Subagent</span>"
            "<span><span class='swatch' style='background:var(--wait)'></span>"
            "Main agent waiting</span></p>"
        )
        for row in timed:
            status = "" if row["status"] == "finished" else f" ({escape(str(row['status']))})"
            body.append(f"<h3>{escape(row['trial'])}{status}</h3>")
            body.append(_timeline(row, scale))
    if data["failed_trials"]:
        body.append("<h2>Failed Trials</h2><ul>")
        body += [
            f"<li>{escape(f['trial'])}: {escape(str(f['reason']))}</li>"
            for f in data["failed_trials"]
        ]
        body.append("</ul>")
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
        f"<body><main>{''.join(body)}</main><div id='tip' role='tooltip'></div>"
        f"<script>{SCRIPT}</script></body></html>\n"
    )
