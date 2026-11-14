"""`report`: compare the Variants from what the results folder holds. No agent, no model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from .judge import MERGED, SEVERITIES
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
]
AGENT_NAMES = {"claude": "Claude Code", "copilot": "Copilot CLI"}
#: Defects a Trial has, by severity, then the nits its Judges listed.
QUALITY = [(s, f"{s.capitalize()} defects", 2) for s in MERGED] + [("nit", "Nits", 2)]


def _round(value: float | None, decimals: int) -> float | int | None:
    if value is None:
        return None
    return int(round(value)) if decimals == 0 else round(float(value), decimals)


def standing(votes: list[str]) -> str:
    """Whether a Trial has a Defect, from its Judges' answers: more yes than no,
    more no than yes, or neither."""
    yes, no = votes.count("yes"), votes.count("no")
    if yes != no:
        return "present" if yes > no else "absent"
    return "disputed" if votes else "unchecked"


def _judging(saved: dict) -> dict:
    return {key: saved.get(key) for key in ("status", "reason", "cost_usd", "seconds")}


def trial_row(trial, judges: list[str], defects: list[dict] | None) -> dict[str, Any]:
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
        "agent": status.get("agent", "claude"),
        # Trials saved before Copilot was an agent hold only `claude_version`.
        "agent_version": status.get("agent_version") or status.get("claude_version"),
        "max_active": timings.get("max_active"),
        "measured": bool(m),
        "cost_usd": m.get("cost_usd"),
        "cost_basis": m.get("cost_basis"),
        "tokens_total": (m.get("tokens") or {}).get("total"),
        "total_minutes": m["total_seconds"] / 60 if "total_seconds" in m else None,
        "wait_minutes": m["wait_seconds"] / 60 if "wait_seconds" in m else None,
        "subagents": len(m["subagents"]) if "subagents" in m else None,
        "prod_added": line_counts.get("prod_added"),
        "prod_removed": line_counts.get("prod_removed"),
        "test_added": line_counts.get("test_added"),
        "test_removed": line_counts.get("test_removed"),
        "checks_passed": bool(checks.get("passed")) if checks.get("ran") else None,
        "final_message": _text(trial.folder / "summary.md"),
        "changed_nothing": _changed_nothing(trial.folder),
        "findings": {},
        "verdicts": {},
        "defect_checks": {},
        "defects": {},
    }
    for key, _, decimals in BUILD + OUTPUT:
        row[key] = _round(row[key], decimals)
    votes: dict[str, list[str]] = {d["id"]: [] for d in defects or []}
    for name in judges:
        verdict = read_json(trial.folder / "verdicts" / f"{name}.json")
        if verdict is not None:
            row["verdicts"][name] = _judging(verdict)
            # A Verdict from before Findings had severities holds none to count.
            if verdict.get("status") == "ok" and "findings" in verdict:
                found = [f.get("severity") for f in verdict["findings"]]
                row["findings"][name] = {s: found.count(s) for s in SEVERITIES}
        checked = read_json(trial.folder / "defect-checks" / f"{name}.json")
        if checked is not None:
            row["defect_checks"][name] = _judging(checked)
            for defect, answer in (checked.get("answers") or {}).items():
                if defect in votes:
                    votes[defect].append(answer.get("present"))
    row["defects"] = {defect: standing(answers) for defect, answers in votes.items()}
    # How many Defects of each severity the Trial has. Unknown until the Defects
    # are merged and, when there are any, until a Judge has checked this Trial.
    known = defects is not None and (not defects or any(v != [] for v in votes.values()))
    for severity in MERGED:
        row[severity] = (
            sum(
                1
                for d in defects or []
                if d["severity"] == severity and row["defects"][d["id"]] == "present"
            )
            if known
            else None
        )
    nits = [found["nit"] for found in row["findings"].values()]
    row["nit"] = round(mean(nits), 2) if nits else None
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
    main: dict = next((a for a in m.get("by_agent", []) if a.get("agent") == "main"), {})
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


def _severity_order(defect: dict) -> tuple[int, int]:
    number = defect["id"].lstrip("D")
    return SEVERITIES.index(defect["severity"]), int(number) if number.isdigit() else 0


def build_report(settings: Settings, folder: Path) -> dict:
    experiment = read_json(folder / "experiment.json") or {}
    # Only the Judges in the settings file: a results folder can still hold the
    # Verdicts of Judges since removed, which would count as votes and as cost.
    judges = [j.name for j in settings.judges]
    merged = read_json(folder / "defects.json")
    defects = sorted(merged.get("defects") or [], key=_severity_order) if merged else None
    rows = [trial_row(t, judges, defects) for t in saved_trials(folder)]

    order = [v.name for v in settings.variants]
    order += sorted({r["variant"] for r in rows} - set(order))
    variants = []
    for name in order:
        mine = [r for r in rows if r["variant"] == name]
        if not mine:
            continue
        done = [r for r in mine if r["status"] == "finished"]
        variants.append(
            {
                "name": name,
                "trials": len(mine),
                "finished": len(done),
                "build": {k: aggregate([r[k] for r in done], d) for k, _, d in BUILD},
                "output": {k: aggregate([r[k] for r in done], d) for k, _, d in OUTPUT},
                "quality": {
                    # Of every Trial, so one that failed to finish counts as not passing.
                    "checks": {
                        "passed": sum(1 for r in mine if r["checks_passed"]),
                        "ran": sum(1 for r in mine if r["checks_passed"] is not None),
                        "trials": len(mine),
                    },
                    **{k: aggregate([r[k] for r in done], d) for k, _, d in QUALITY},
                },
            }
        )

    defect_rows = []
    for defect in defects or []:
        standings = {}
        for v in variants:
            found = [
                r["defects"].get(defect["id"], "unchecked")
                for r in rows
                if r["variant"] == v["name"] and r["status"] == "finished"
            ]
            standings[v["name"]] = {
                "present": found.count("present"),
                "disputed": found.count("disputed"),
                "checked": len(found) - found.count("unchecked"),
            }
        defect_rows.append(
            {
                "id": defect["id"],
                "severity": defect["severity"],
                "title": defect["title"],
                "description": defect["description"],
                "findings": len(defect["findings"]),
                "found_by": sorted({key.split("/")[1] for key in defect["findings"]}),
                "variants": standings,
            }
        )

    judge_rows = []
    for j in judges:
        verdicts = [r["verdicts"][j] for r in rows if j in r["verdicts"]]
        checks = [r["defect_checks"][j] for r in rows if j in r["defect_checks"]]
        merges = [_judging(merged)] if merged and merged.get("judge") == j else []
        every = verdicts + checks + merges
        costs = [v["cost_usd"] for v in every if v["cost_usd"] is not None]
        seconds = [v["seconds"] for v in every if v["seconds"] is not None]
        config = next((x for x in settings.judges if x.name == j), None)
        judge_rows.append(
            {
                "name": j,
                "type": config.type if config else None,
                "model": config.model if config else None,
                "verdicts": sum(1 for v in verdicts if v["status"] == "ok"),
                "checks": sum(1 for v in checks if v["status"] == "ok"),
                "failed": sum(1 for v in every if v["status"] != "ok"),
                "cost_usd": _round(sum(costs), 4) if costs else None,
                "minutes": _round(sum(seconds) / 60, 2) if seconds else None,
            }
        )

    failed_judging = [
        {"trial": r["trial"], "judge": j, "step": step, "reason": v["reason"]}
        for step, key in (("verdict", "verdicts"), ("defect check", "defect_checks"))
        for r in rows
        for j, v in r[key].items()
        if v["status"] != "ok"
    ]
    if merged and merged.get("status") != "ok":
        failed_judging.append(
            {
                "trial": None,
                "judge": merged.get("judge"),
                "step": "merge",
                "reason": merged["reason"],
            }
        )

    models = [v.model for v in settings.variants] + [j.model for j in settings.judges]
    versions = {
        kind: sorted(
            {r["agent_version"] for r in rows if r["agent"] == kind and r["agent_version"]}
        )
        for kind in AGENT_NAMES
    }
    return {
        "experiment": experiment.get("name", settings.name),
        "results_folder": str(folder),
        "auth": experiment.get("auth", settings.auth),
        "claude_versions": versions["claude"],
        "copilot_versions": versions["copilot"],
        "flags": {
            "ran_alongside_others": [
                {"trial": r["trial"], "max_active": r["max_active"]}
                for r in rows
                if (r["max_active"] or 1) > 1
            ],
            "mixed_claude_versions": len(versions["claude"]) > 1,
            "mixed_copilot_versions": len(versions["copilot"]) > 1,
            "subscription_login": experiment.get("auth", settings.auth) == "subscription",
            # Claude Code Trials are priced from list prices, Copilot Trials from its credits.
            "mixed_cost_bases": len({r["cost_basis"] for r in rows if r["cost_basis"]}) > 1,
            # A Judge tends to go easy on work from its own model family.
            "judges_share_the_builders_family": bool(settings.judges)
            and all("claude" in model.lower() for model in models),
        },
        "variants": variants,
        "defects": defect_rows if defects is not None else None,
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
        "failed_judging": failed_judging,
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


@dataclass
class Table:
    """One of the report's tables. The first value of each row is its row header."""

    title: str
    head: list[str]
    rows: list[list[str]]
    note: str = ""


def _summary(variants: list[dict], group: str, columns) -> list[list[str]]:
    return [
        [v["name"], f"{v['finished']}/{v['trials']}"] + [cell(v[group][k]) for k, _, _ in columns]
        for v in variants
    ]


def _checks_cell(checks: dict) -> str:
    return f"{checks['passed']}/{checks['trials']}" if checks["ran"] else "–"


def _defect_cell(found: dict) -> str:
    if not found["checked"]:
        return "–"
    disputed = f" ({found['disputed']} disputed)" if found["disputed"] else ""
    return f"{found['present']}/{found['checked']}{disputed}"


def tables(data: dict) -> list[Table]:
    """Every table of the report, in order, for the Markdown and the HTML alike."""
    variants = data["variants"]
    labels = ["Variant", "Finished"]
    found = [
        Table(
            "Build",
            labels + [label for _, label, _ in BUILD],
            _summary(variants, "build", BUILD),
            "Each cell is the average over finished Trials, with the lowest and highest in "
            "brackets. Failed Trials are left out of the averages.",
        ),
        Table(
            "Output",
            labels + [label for _, label, _ in OUTPUT],
            _summary(variants, "output", OUTPUT),
        ),
        Table(
            "Quality",
            labels + ["Checks passed"] + [label for _, label, _ in QUALITY],
            [
                [v["name"], f"{v['finished']}/{v['trials']}", _checks_cell(v["quality"]["checks"])]
                + [cell(v["quality"][k]) for k, _, _ in QUALITY]
                for v in variants
            ],
            "Checks passed counts every Trial, so one that failed to finish counts against "
            "its Variant. The Defect columns are the Defects one Trial has, on average. Nits "
            "are what one Judge listed for one Trial, on average.",
        ),
    ]
    if data["defects"]:
        found.append(
            Table(
                "Defects",
                ["Defect", "Severity", "What is wrong", "Found by"] + [v["name"] for v in variants],
                [
                    [d["id"], d["severity"], d["title"].replace("|", "/"), ", ".join(d["found_by"])]
                    + [_defect_cell(d["variants"][v["name"]]) for v in variants]
                    for d in data["defects"]
                ],
                "Each Variant's column is the Trials that have the Defect, of the Trials "
                "checked for it. A Trial has a Defect when more of its Judges say it does than "
                "say it does not; an even split is disputed.",
            )
        )
    if data["judges"]:
        found.append(
            Table(
                "Judges",
                ["Judge", "Type", "Model", "Verdicts", "Defect checks", "Failed", "Cost ($)"]
                + ["Time (min)"],
                [
                    [j["name"], j["type"] or "–", j["model"] or "–", str(j["verdicts"])]
                    + [str(j["checks"]), str(j["failed"]), num(j["cost_usd"]), num(j["minutes"])]
                    for j in data["judges"]
                ],
                "Judging cost and time, kept apart from the build numbers.",
            )
        )
    return found


def failed_judging_lines(data: dict) -> list[str]:
    out = []
    for f in data["failed_judging"]:
        if f["step"] == "merge":
            out.append(f"Merging the Defects, by {f['judge']}: {f['reason']}")
        elif f["step"] == "verdict":
            out.append(f"{f['trial']} by {f['judge']}: {f['reason']}")
        else:
            out.append(f"{f['trial']} by {f['judge']} ({f['step']}): {f['reason']}")
    return out


def flag_lines(data: dict) -> list[str]:
    out = []
    if data["flags"]["subscription_login"]:
        out.append(
            "- Login: **subscription**. Subscription rate limits can slow Trials down, "
            "so trust time less than cost and quality."
        )
    elif data["auth"]:
        out.append("- Login: **API key**.")
    alongside = data["flags"]["ran_alongside_others"]
    if alongside:
        names = ", ".join(f"{a['trial']} ({a['max_active']} at once)" for a in alongside)
        out.append(
            f"- **Ran alongside other Trials:** {names}. Time comparisons between Trials "
            "that ran at once are weaker than cost and quality comparisons."
        )
    for kind, name in AGENT_NAMES.items():
        versions = data[f"{kind}_versions"]
        if len(versions) > 1:
            out.append(
                f"- **Mixed {name} versions:** "
                + ", ".join(versions)
                + ". Trials built by different versions may not be comparable."
            )
        elif versions:
            out.append(f"- {name} version: {versions[0]}.")
    if data["flags"]["mixed_cost_bases"]:
        out.append(
            "- **Costs are on two bases.** Claude Code Trials are priced from list prices, "
            "Copilot Trials from the AI credits Copilot charged, so compare cost between "
            "the two with care."
        )
    if data["flags"]["judges_share_the_builders_family"]:
        out.append(
            "- **Every Judge is a Claude model, like the builders.** A Judge tends to go "
            "easy on work from its own model family, so add a Judge from another provider."
        )
    return out


def markdown(data: dict) -> str:
    out = [f"# {data['experiment']}", ""]
    out += flag_lines(data) + [""]
    for table in tables(data):
        out += [f"## {table.title}", ""]
        if table.note:
            out += [table.note, ""]
        out += ["| " + " | ".join(table.head) + " |", "|" + "---|" * len(table.head)]
        out += ["| " + " | ".join(row) + " |" for row in table.rows]
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
    if data["failed_judging"]:
        out += ["## Failed judging", ""]
        out += [f"- {line}" for line in failed_judging_lines(data)]
        out += [""]
    return "\n".join(out)


def report(settings: Settings, folder: Path) -> list[Path]:
    from .html_report import html

    data = build_report(settings, folder)
    write_json(folder / "report.json", data)
    (folder / "report.md").write_text(markdown(data))
    (folder / "report.html").write_text(html(data))
    return [folder / "report.json", folder / "report.md", folder / "report.html"]
