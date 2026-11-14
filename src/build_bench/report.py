"""`report`: compare the Variants from what the results folder holds. No agent, no model."""

from __future__ import annotations

import random
from dataclasses import dataclass
from itertools import combinations
from math import comb
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
#: What the Differences table compares between two Variants; lower is better in each.
COMPARED = [
    ("defects", "Defects per Trial", 2),
    ("cost_usd", "Cost ($)", 4),
    ("total_minutes", "Time (min)", 2),
]
#: A difference is clear when chance alone gives one as large less than 1 time
#: in 20, and possible when less than 1 time in 5.
CLEAR, POSSIBLE = 0.05, 0.2
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


def weight(chance: float | None) -> str | None:
    """How a difference stands against chance: clear, possible or within noise."""
    if chance is None:
        return None
    if chance < CLEAR:
        return "clear"
    return "possible" if chance < POSSIBLE else "within noise"


def shuffle_chance(a: list[float], b: list[float]) -> float | None:
    """How often dealing these Trials' numbers out to the two Variants at random
    gives averages at least as far apart as they are. Every way of dealing them
    is tried, or 20,000 random ones when there are too many."""
    if not a or not b:
        return None
    pooled, n = a + b, len(a)
    seen = abs(mean(a) - mean(b)) - 1e-9

    def apart(first: tuple[int, ...]) -> bool:
        chosen = set(first)
        one = [pooled[i] for i in chosen]
        other = [v for i, v in enumerate(pooled) if i not in chosen]
        return abs(mean(one) - mean(other)) >= seen

    places = range(len(pooled))
    if comb(len(pooled), n) <= 100_000:
        deals = list(combinations(places, n))
        return round(sum(1 for deal in deals if apart(deal)) / len(deals), 4)
    dealer = random.Random(0)
    deals = [tuple(dealer.sample(places, n)) for _ in range(20_000)]
    # The deal that happened counts as one, so a sample never says "never".
    return round((sum(1 for deal in deals if apart(deal)) + 1) / (len(deals) + 1), 4)


def coin_chance(wins: int, losses: int) -> float | None:
    """How often a fair coin gives a split at least as uneven as this one."""
    n = wins + losses
    if not n:
        return None
    tail = sum(comb(n, k) for k in range(max(wins, losses), n + 1)) / 2**n
    return round(min(1.0, 2 * tail), 4)


def voted_severity(votes: list[str]) -> str | None:
    """The middle of the severities the Judges gave; the worse of the two middle
    ones when their number is even."""
    ranked = sorted((v for v in votes if v in MERGED), key=SEVERITIES.index)
    return ranked[(len(ranked) - 1) // 2] if ranked else None


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
    row["defects_total"] = sum(row[s] for s in MERGED) if known else None
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


def _defects(merged: dict, trials: list, judges: list[str]) -> list[dict]:
    """The Defects, worst first. A Defect is as severe as the Judges voted when
    they said a Trial has it, or as its worst Finding until any has."""
    votes: dict[str, list[str]] = {}
    for trial in trials:
        for name in judges:
            checked = read_json(trial.folder / "defect-checks" / f"{name}.json") or {}
            for defect, answer in (checked.get("answers") or {}).items():
                if answer.get("present") == "yes" and answer.get("severity"):
                    votes.setdefault(defect, []).append(answer["severity"])
    found = []
    for defect in merged.get("defects") or []:
        mine = votes.get(defect["id"], [])
        found.append(
            {
                **defect,
                "severity": voted_severity(mine) or defect["severity"],
                "found_severity": defect["severity"],
                "severity_votes": len(mine),
            }
        )
    return sorted(found, key=_severity_order)


def _differences(variants: list[dict], rows: list[dict]) -> list[dict]:
    """For each pair of Variants, how far apart their averages are and how that
    stands against the spread between Trials of one Variant."""
    out = []
    for a, b in combinations([v["name"] for v in variants], 2):
        measures: dict[str, dict | None] = {}
        for key, _, decimals in COMPARED:
            column = "defects_total" if key == "defects" else key
            mine = [
                [r[column] for r in rows if r["variant"] == name and r["status"] == "finished"]
                for name in (a, b)
            ]
            one, other = ([v for v in values if v is not None] for values in mine)
            if not one or not other:
                measures[key] = None
                continue
            gap = mean(other) - mean(one)
            chance = shuffle_chance(one, other)
            measures[key] = {
                "lower": None if abs(gap) < 1e-9 else (a if gap > 0 else b),
                "by": _round(abs(gap), max(decimals, 2)),
                "chance": chance,
                "weight": weight(chance),
            }
        out.append({"variants": [a, b], "measures": measures})
    return out


def _comparisons(folder: Path, variants: list[dict], rows: list[dict], judges: list[str]):
    """Every saved Comparison by a Judge in the settings, and the count of them
    for each pair of Variants."""
    variant_of = {r["trial"]: r["variant"] for r in rows}
    saved = []
    for name in judges:
        for path in sorted((folder / "comparisons" / name).glob("*.json")):
            record = read_json(path) or {}
            if record.get("first") in variant_of and record.get("second") in variant_of:
                saved.append({**record, "judge": name})
    # Two Judges asked about the same two Trials are not two independent answers,
    # so each pair of Trials counts once: for the Trial more of its Judges preferred.
    votes: dict[frozenset[str], list[str | None]] = {}
    for c in saved:
        if c.get("status") == "ok":
            votes.setdefault(frozenset((c["first"], c["second"])), []).append(c.get("better"))
    summary = []
    for a, b in combinations([v["name"] for v in variants], 2):
        wins = {a: 0, b: 0}
        compared = 0
        for pair, answers in votes.items():
            if {variant_of[t] for t in pair} != {a, b}:
                continue
            compared += 1
            counts = sorted(((answers.count(t), t) for t in pair), reverse=True)
            if counts[0][0] > counts[1][0]:
                wins[variant_of[counts[0][1]]] += 1
        if not compared:
            continue
        chance = coin_chance(wins[a], wins[b])
        summary.append(
            {
                "variants": [a, b],
                "compared": compared,
                "better": wins,
                "same": compared - wins[a] - wins[b],
                "ahead": None if wins[a] == wins[b] else max(wins, key=lambda n: wins[n]),
                "chance": chance,
                "weight": weight(chance),
            }
        )
    return saved, summary


def build_report(settings: Settings, folder: Path) -> dict:
    experiment = read_json(folder / "experiment.json") or {}
    # Only the Judges in the settings file: a results folder can still hold the
    # Verdicts of Judges since removed, which would count as votes and as cost.
    judges = [j.name for j in settings.judges]
    merged = read_json(folder / "defects.json")
    trials = saved_trials(folder)
    defects = _defects(merged, trials, judges) if merged else None
    rows = [trial_row(t, judges, defects) for t in trials]

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
        checked = sum(s["checked"] for s in standings.values())
        defect_rows.append(
            {
                "id": defect["id"],
                "severity": defect["severity"],
                "found_severity": defect["found_severity"],
                "severity_votes": defect["severity_votes"],
                # A Defect every Trial has says nothing about which Variant is better.
                "in_every_trial": checked > 1
                and all(0 < s["present"] == s["checked"] for s in standings.values()),
                "title": defect["title"],
                "description": defect["description"],
                "findings": len(defect["findings"]),
                "found_by": sorted({key.split("/")[1] for key in defect["findings"]}),
                "variants": standings,
            }
        )

    compared, comparison_rows = _comparisons(folder, variants, rows, judges)
    judge_rows = []
    for j in judges:
        verdicts = [r["verdicts"][j] for r in rows if j in r["verdicts"]]
        checks = [r["defect_checks"][j] for r in rows if j in r["defect_checks"]]
        merges = [_judging(merged)] if merged and merged.get("judge") == j else []
        pairs = [_judging(c) for c in compared if c["judge"] == j]
        every = verdicts + checks + merges + pairs
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
                "comparisons": sum(1 for v in pairs if v["status"] == "ok"),
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
    failed_judging += [
        {
            "trial": f"{c['first']} with {c['second']}",
            "judge": c["judge"],
            "step": "comparison",
            "reason": c.get("reason"),
        }
        for c in compared
        if c.get("status") != "ok"
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
        "differences": _differences(variants, rows),
        "defects": defect_rows if defects is not None else None,
        "comparisons": comparison_rows,
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


def _difference_cell(found: dict | None) -> str:
    if found is None:
        return "–"
    if found["lower"] is None:
        return "the same"
    return f"{found['lower']} lower by {num(found['by'])}: {found['weight']}"


def _defect_table(title: str, defects: list[dict], variants: list[dict], note: str) -> Table:
    return Table(
        title,
        ["Defect", "Severity", "What is wrong", "Found by"] + [v["name"] for v in variants],
        [
            [d["id"], d["severity"], d["title"].replace("|", "/"), ", ".join(d["found_by"])]
            + [_defect_cell(d["variants"][v["name"]]) for v in variants]
            for d in defects
        ],
        note,
    )


def _comparison_result(c: dict) -> str:
    return "even" if c["ahead"] is None else f"{c['ahead']} ahead: {c['weight']}"


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
    if any(any(d["measures"].values()) for d in data["differences"]):
        found.append(
            Table(
                "Differences",
                ["Variants"] + [label for _, label, _ in COMPARED],
                [
                    [" and ".join(d["variants"])]
                    + [_difference_cell(d["measures"][k]) for k, _, _ in COMPARED]
                    for d in data["differences"]
                ],
                "Each cell names the Variant with the lower average and by how much, then "
                "how that stands against the spread between Trials of the same Variant. "
                "Clear: dealing the Trials out to the two Variants at random gives a "
                "difference this large less than 1 time in 20. Possible: less than 1 time "
                "in 5. Within noise: more often than that, so do not read it as a real "
                "difference. With 3 Trials each, nothing can come out clear; it takes 4.",
            )
        )
    differing = [d for d in data["defects"] or [] if not d["in_every_trial"]]
    shared = [d for d in data["defects"] or [] if d["in_every_trial"]]
    severity = (
        " A Defect is as severe as the Judges who found it in a Trial voted: the middle "
        "of their votes."
    )
    if differing:
        found.append(
            _defect_table(
                "Defects",
                differing,
                variants,
                "The Defects some Trials have and others do not. Each Variant's column is "
                "the Trials that have the Defect, of the Trials checked for it. A Trial has "
                "a Defect when more of its Judges say it does than say it does not; an even "
                "split is disputed." + severity,
            )
        )
    if shared:
        found.append(
            _defect_table(
                "Defects in every Trial",
                shared,
                variants,
                "Every Trial checked has these, so they say nothing about which Variant is "
                "better. They usually point at a gap in the plan."
                + ("" if differing else severity),
            )
        )
    if data["comparisons"]:
        found.append(
            Table(
                "Comparisons",
                ["Variants", "Pairs compared", "First named better", "Second named better"]
                + ["Same", "Result"],
                [
                    [" and ".join(c["variants"]), str(c["compared"])]
                    + [str(c["better"][name]) for name in c["variants"]]
                    + [str(c["same"]), _comparison_result(c)]
                    for c in data["comparisons"]
                ],
                "A Judge was shown two Trials, one of each Variant, without their names and "
                "said which it would rather ship. A pair of Trials counts once, for the one "
                "more of its Judges preferred. The result weighs the split against a coin "
                "toss, in the same words as the Differences table.",
            )
        )
    if data["judges"]:
        found.append(
            Table(
                "Judges",
                ["Judge", "Type", "Model", "Verdicts", "Defect checks", "Comparisons", "Failed"]
                + ["Cost ($)", "Time (min)"],
                [
                    [j["name"], j["type"] or "–", j["model"] or "–", str(j["verdicts"])]
                    + [str(j["checks"]), str(j["comparisons"]), str(j["failed"])]
                    + [num(j["cost_usd"]), num(j["minutes"])]
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
