"""`report`: compare the Variants from what the results folder holds. No agent, no model."""

from __future__ import annotations

from pathlib import Path
from statistics import mean
from typing import Any

from .judge import SCORES
from .results import read_json, saved_trials, write_json
from .settings import Settings

#: (key, label, decimals) for each per-Trial number, in table order.
BUILD = [
    ("cost_usd", "Cost ($)", 4),
    ("tokens_total", "Tokens", 0),
    ("total_minutes", "Time (min)", 2),
    ("wait_minutes", "Waiting on subagents (min)", 2),
    ("subagents", "Subagents", 0),
]
OUTPUT = [
    ("prod_added", "Prod lines +", 0),
    ("prod_removed", "Prod lines -", 0),
    ("test_added", "Test lines +", 0),
    ("test_removed", "Test lines -", 0),
    ("checks_passed", "Checks passed (share)", 2),
    ("criteria_ticked", "Criteria ticked", 0),
]
SCORE_COLUMNS = [(s, s.replace("_", " ").capitalize(), 2) for s in SCORES] + [("mean", "Mean", 2)]


def _round(value: float | None, decimals: int) -> float | int | None:
    if value is None:
        return None
    return int(round(value)) if decimals == 0 else round(float(value), decimals)


def trial_row(trial, judges: list[str]) -> dict[str, Any]:
    status = trial.status or {}
    m = read_json(trial.folder / "measurements.json") or {}
    timings = read_json(trial.folder / "timings.json") or {}
    line_counts = m.get("lines") or {}
    checks = m.get("checks") or {}
    row: dict[str, Any] = {
        "trial": status.get("trial", trial.id),
        "variant": status.get("variant", trial.variant),
        "repeat": status.get("repeat", trial.repeat),
        "status": status.get("status"),
        "reason": status.get("reason"),
        "claude_version": status.get("claude_version"),
        "max_active": timings.get("max_active"),
        "measured": bool(m),
        "cost_usd": m.get("cost_usd"),
        "tokens_total": (m.get("tokens") or {}).get("total"),
        "total_minutes": m["total_seconds"] / 60 if "total_seconds" in m else None,
        "wait_minutes": m["wait_seconds"] / 60 if "wait_seconds" in m else None,
        "subagents": len(m["subagents"]) if "subagents" in m else None,
        "prod_added": line_counts.get("prod_added"),
        "prod_removed": line_counts.get("prod_removed"),
        "test_added": line_counts.get("test_added"),
        "test_removed": line_counts.get("test_removed"),
        "checks_passed": (1.0 if checks.get("passed") else 0.0) if checks.get("ran") else None,
        "criteria_ticked": m.get("criteria_ticked") if m else None,
        "final_message": _text(trial.folder / "summary.md"),
        "changed_nothing": _changed_nothing(trial.folder),
        "scores": {},
        "verdicts": {},
    }
    for key, _, decimals in BUILD + OUTPUT:
        row[key] = _round(row[key], decimals)
    for name in judges:
        verdict = read_json(trial.folder / "verdicts" / f"{name}.json")
        if verdict is None:
            continue
        row["verdicts"][name] = {
            "status": verdict.get("status"),
            "reason": verdict.get("reason"),
            "cost_usd": verdict.get("cost_usd"),
            "seconds": verdict.get("seconds"),
            "bugs": len(verdict.get("bugs") or []),
        }
        if verdict.get("status") == "ok":
            scores = dict(verdict["scores"])
            scores["mean"] = round(mean(scores.values()), 2)
            row["scores"][name] = scores
    timeline = _timeline(m)
    if timeline:
        row["timeline"] = timeline
    return row


def _text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip() or None
    except FileNotFoundError:
        return None


def _changed_nothing(folder: Path) -> bool:
    """The build left no code changes: its diff was saved, and is empty."""
    diff = folder / "diff.patch"
    return diff.is_file() and not diff.read_text(errors="replace").strip()


def _timeline(m: dict) -> dict | None:
    if "total_seconds" not in m:
        return None
    main = next((a for a in m.get("by_agent", []) if a.get("agent") == "main"), {})
    return {
        "total_seconds": m["total_seconds"],
        "main_cost_usd": main.get("cost_usd"),
        "waits": m.get("wait_intervals", []),
        "subagents": [
            {"name": s["name"], "start": s["start"], "end": s["end"], "cost_usd": s.get("cost_usd")}
            for s in m.get("subagents", [])
            if s.get("start") is not None
        ],
    }


def first_paragraph(text: str, limit: int = 400) -> str:
    """The opening paragraph of an agent's final message, on one line."""
    para = " ".join(text.strip().split("\n\n", 1)[0].split())
    return para if len(para) <= limit else para[: limit - 1].rstrip() + "…"


def aggregate(values: list[float | int | None], decimals: int) -> dict:
    present = [v for v in values if v is not None]
    if not present:
        return {"mean": None, "min": None, "max": None, "n": 0}
    return {
        "mean": _round(mean(present), max(decimals, 2)),
        "min": _round(min(present), decimals),
        "max": _round(max(present), decimals),
        "n": len(present),
    }


def build_report(settings: Settings, folder: Path) -> dict:
    experiment = read_json(folder / "experiment.json") or {}
    judges = [j.name for j in settings.judges]
    for trial in saved_trials(folder):
        for f in (trial.folder / "verdicts").glob("*.json"):
            if f.stem not in judges:
                judges.append(f.stem)
    rows = [trial_row(t, judges) for t in saved_trials(folder)]

    order = [v.name for v in settings.variants]
    order += sorted({r["variant"] for r in rows} - set(order))
    variants = []
    for name in order:
        mine = [r for r in rows if r["variant"] == name]
        if not mine:
            continue
        done = [r for r in mine if r["status"] == "finished"]
        entry: dict[str, Any] = {
            "name": name,
            "trials": len(mine),
            "finished": len(done),
            "build": {k: aggregate([r[k] for r in done], d) for k, _, d in BUILD},
            "output": {k: aggregate([r[k] for r in done], d) for k, _, d in OUTPUT},
            "scores": {},
        }
        for j in judges:
            scored = [r["scores"][j] for r in done if j in r["scores"]]
            entry["scores"][j] = {
                k: aggregate([s[k] for s in scored], d) for k, _, d in SCORE_COLUMNS
            }
        variants.append(entry)

    judge_rows = []
    for j in judges:
        verdicts = [r["verdicts"][j] for r in rows if j in r["verdicts"]]
        costs = [v["cost_usd"] for v in verdicts if v["cost_usd"] is not None]
        seconds = [v["seconds"] for v in verdicts if v["seconds"] is not None]
        config = next((x for x in settings.judges if x.name == j), None)
        judge_rows.append(
            {
                "name": j,
                "type": config.type if config else None,
                "model": config.model if config else None,
                "verdicts": sum(1 for v in verdicts if v["status"] == "ok"),
                "failed": sum(1 for v in verdicts if v["status"] != "ok"),
                "cost_usd": _round(sum(costs), 4) if costs else None,
                "minutes": _round(sum(seconds) / 60, 2) if seconds else None,
            }
        )

    versions = sorted({r["claude_version"] for r in rows if r["claude_version"]})
    return {
        "experiment": experiment.get("name", settings.name),
        "results_folder": str(folder),
        "auth": experiment.get("auth", settings.auth),
        "claude_versions": versions,
        "flags": {
            "ran_alongside_others": [
                {"trial": r["trial"], "max_active": r["max_active"]}
                for r in rows
                if (r["max_active"] or 1) > 1
            ],
            "mixed_claude_versions": len(versions) > 1,
            "subscription_login": experiment.get("auth", settings.auth) == "subscription",
        },
        "variants": variants,
        "judges": judge_rows,
        "failed_trials": [
            {"trial": r["trial"], "reason": r["reason"], "final_message": r["final_message"]}
            for r in rows
            if r["status"] != "finished"
        ],
        "empty_trials": [
            {"trial": r["trial"], "final_message": r["final_message"]}
            for r in rows
            if r["status"] == "finished" and r["changed_nothing"]
        ],
        "failed_verdicts": [
            {"trial": r["trial"], "judge": j, "reason": v["reason"]}
            for r in rows
            for j, v in r["verdicts"].items()
            if v["status"] != "ok"
        ],
        "trials": rows,
    }


# --- Markdown -----------------------------------------------------------------


def num(value: float | int | None) -> str:
    if value is None:
        return "–"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def cell(agg: dict) -> str:
    if not agg["n"]:
        return "–"
    if agg["min"] == agg["max"]:
        return num(agg["mean"])
    return f"{num(agg['mean'])} ({num(agg['min'])}–{num(agg['max'])})"


def _table(variants: list[dict], group: str, columns, judge: str | None = None) -> list[str]:
    head = ["Variant", "Finished"] + [label for _, label, _ in columns]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for v in variants:
        values = v[group][judge] if judge else v[group]
        cells = [v["name"], f"{v['finished']}/{v['trials']}"]
        cells += [cell(values[key]) for key, _, _ in columns]
        out.append("| " + " | ".join(cells) + " |")
    return out


def flag_lines(data: dict) -> list[str]:
    out = []
    if data["flags"]["subscription_login"]:
        out.append(
            "- Login: **subscription**. Subscription rate limits can slow Trials down, "
            "so trust time less than cost and quality."
        )
    else:
        out.append("- Login: **API key**.")
    alongside = data["flags"]["ran_alongside_others"]
    if alongside:
        names = ", ".join(f"{a['trial']} ({a['max_active']} at once)" for a in alongside)
        out.append(
            f"- **Ran alongside other Trials:** {names}. Time comparisons between Trials "
            "that ran at once are weaker than cost and quality comparisons."
        )
    if data["flags"]["mixed_claude_versions"]:
        out.append(
            "- **Mixed Claude Code versions:** "
            + ", ".join(data["claude_versions"])
            + ". Trials built by different versions may not be comparable."
        )
    elif data["claude_versions"]:
        out.append(f"- Claude Code version: {data['claude_versions'][0]}.")
    return out


def markdown(data: dict) -> str:
    out = [f"# {data['experiment']}", ""]
    out += flag_lines(data) + [""]
    out += [
        "Each cell is the average over finished Trials, with the lowest and highest "
        "in brackets. Failed Trials are left out.",
        "",
        "## Build",
        "",
    ]
    out += _table(data["variants"], "build", BUILD) + ["", "## Output", ""]
    out += _table(data["variants"], "output", OUTPUT) + [""]
    for j in data["judges"]:
        out += [f"## Scores: {j['name']}", ""]
        out += _table(data["variants"], "scores", SCORE_COLUMNS, judge=j["name"]) + [""]
    if data["judges"]:
        out += [
            "## Judges",
            "",
            "Judging cost and time, kept apart from the build numbers.",
            "",
            "| Judge | Type | Model | Verdicts | Failed | Cost ($) | Time (min) |",
            "|---|---|---|---|---|---|---|",
        ]
        for j in data["judges"]:
            out.append(
                f"| {j['name']} | {j['type'] or '–'} | {j['model'] or '–'} | {j['verdicts']} "
                f"| {j['failed']} | {num(j['cost_usd'])} | {num(j['minutes'])} |"
            )
        out.append("")
    if data["failed_trials"]:
        out += ["## Failed Trials", ""]
        for f in data["failed_trials"]:
            out.append(f"- {f['trial']}: {f['reason']}")
            if f["final_message"]:
                out.append(f"  - The agent's last words: “{first_paragraph(f['final_message'])}”")
        out.append("")
    if data["empty_trials"]:
        out += [
            "## Trials that changed nothing",
            "",
            "These Trials finished but left no code changes. Their numbers are in the averages.",
            "",
        ]
        for f in data["empty_trials"]:
            words = f["final_message"]
            out.append(
                f"- {f['trial']}: “{first_paragraph(words)}”"
                if words
                else f"- {f['trial']}: the agent wrote no final message."
            )
        out.append("")
    if data["failed_verdicts"]:
        out += ["## Failed Verdicts", ""]
        out += [f"- {f['trial']} by {f['judge']}: {f['reason']}" for f in data["failed_verdicts"]]
        out += [""]
    return "\n".join(out)


def report(settings: Settings, folder: Path) -> list[Path]:
    from .html_report import html

    data = build_report(settings, folder)
    write_json(folder / "report.json", data)
    (folder / "report.md").write_text(markdown(data))
    (folder / "report.html").write_text(html(data))
    return [folder / "report.json", folder / "report.md", folder / "report.html"]
