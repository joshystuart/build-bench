"""`insights`: a model reads the report and writes the words for insights.html.

The page's charts and counts come from the report's own numbers; the model
writes only the words around them: a headline, the heading over each chart,
plain titles for the Defects, what each subagent was doing, the caveats and a
recommendation. Its reply is saved in `insights.json` with a hash of
everything it was shown, so `report` draws the page again without asking, and
`insights` asks again only once the results have changed.
"""

from __future__ import annotations

import tempfile
from importlib import resources
from itertools import combinations
from pathlib import Path
from typing import Any

from .agent import Agents
from .judge import VerdictError, _ask_agent, _ask_api, _attempt, _record, _sha, reply_json
from .report import build_report, markdown
from .results import read_json
from .run import in_parallel
from .settings import Settings

#: What the model is shown of the plan and of each Variant's prompt, at most.
PLAN_LIMIT = 24_000
PROMPT_LIMIT = 6_000
#: The headings the model writes, one over each part of the page.
SECTIONS = ("inputs", "quality", "cost", "time", "runs", "shared", "differing", "comparisons")
#: The most stats, roles, caveats and reasons the page has room for.
MOST = {"stats": 3, "roles": 5, "caveats": 4, "why": 4, "next": 4}


def instructions() -> str:
    return (resources.files("build_bench") / "prompts" / "insights.md").read_text()


def digest(settings: Settings, folder: Path, data: dict) -> str:
    """Everything the model is shown: the setups, numbers worked out for it, the
    report, the subagents' names and the plan."""
    parts = [
        f"# Experiment {data['experiment']}\n",
        *_setups(settings, folder, data),
        *_worked_out(data),
        "# The results\n",
        markdown(data),
        *_subagents(data),
        *_plan(folder),
    ]
    return "\n".join(parts)


def input_sha(settings: Settings, folder: Path, data: dict) -> str:
    return _sha(instructions() + digest(settings, folder, data))


def saved_words(settings: Settings, folder: Path, data: dict) -> dict | None:
    """The saved insights, when they were written from these very results."""
    saved = read_json(folder / "insights.json") or {}
    if saved.get("status") != "ok" or saved.get("input_sha") != input_sha(settings, folder, data):
        return None
    return saved


def insights(settings: Settings, folder: Path, *, fresh: bool = False, say=print) -> bool:
    """Ask the writer for the words, unless the saved ones were written from these
    results by the same model. Returns False when asking failed."""
    j = settings.writer
    assert j is not None
    data = build_report(settings, folder)
    shown = digest(settings, folder, data)
    sha = _sha(instructions() + shown)
    target = folder / "insights.json"
    saved = read_json(target) or {}
    if (
        not fresh
        and saved.get("status") == "ok"
        and saved.get("input_sha") == sha
        and saved.get("model") == j.model
    ):
        say("The results have not changed since the insights were written (--fresh asks again).")
        return True
    say(f"Asking {j.model} to write the insights…")

    def ask() -> tuple[str, float | None]:
        if j.type == "api":
            return _ask_api(settings, j, instructions(), shown, agents)
        with tempfile.TemporaryDirectory(prefix="build-bench-insights-") as tmp:
            work = Path(tmp) / "insights"
            work.mkdir()
            prompt = f"{instructions()}\n\n---\n\n{shown}"
            return _ask_agent(settings, j, prompt, work, target.with_suffix(""), agents)

    def parse(reply: str) -> dict:
        return {"words": check_words(reply_json(reply, "headline"), data)}

    agents = Agents(settings.env)
    record = _record(j, input_sha=sha, words=None)
    # Asked as `judge` asks, so Ctrl+C stops an Agent writer cleanly. `_attempt`
    # fills in `record` and saves it.
    in_parallel(lambda r: _attempt(r, target, ask, parse), [record], 1, agents, say)
    if record["status"] != "ok":
        say(f"error: the insights failed: {record['reason']}")
        return False
    cost = record["cost_usd"]
    say(
        f"Insights written by {j.model} in {record['seconds']:.0f}s"
        + ("" if cost is None else f" for ${cost:.4f}")
    )
    return True


# --- What the model is shown ----------------------------------------------------


def _setups(settings: Settings, folder: Path, data: dict) -> list[str]:
    """Each Variant's agent, model and skills, and the prompt its Trials were given."""
    configured = {v.name: v for v in settings.variants}
    parts = ["## The Variants\n"]
    for v in data["variants"]:
        mine = configured.get(v["name"])
        skills = sorted(p.name for p in (folder / "skills" / v["name"]).glob("*") if p.is_dir())
        facts = [f"{v['trials']} Trial(s)"]
        if mine is not None:
            facts = [f"{mine.agent} agent", f"model {mine.model}"]
            facts += [f"effort {mine.effort}"] if mine.effort else []
            facts += [f"{v['trials']} Trial(s)"]
        facts += [f"skills: {', '.join(skills)}"] if skills else []
        parts.append(f"### {v['name']}\n\n{'; '.join(facts)}.\n")
        prompt = _first_prompt(folder, v["name"])
        if prompt:
            parts.append(f"Its prompt:\n\n```\n{_cut(prompt, PROMPT_LIMIT)}\n```\n")
    if settings.judges:
        judges = ", ".join(f"{j.name} ({j.type}, {j.model})" for j in settings.judges)
        parts.append(f"## The Judges\n\n{judges}.\n")
    return parts


def _first_prompt(folder: Path, variant: str) -> str | None:
    for path in sorted((folder / "trials").glob(f"{variant}-*/prompt.md")):
        if path.parent.name.rpartition("-")[0] == variant:
            return path.read_text(errors="replace").strip()
    return None


def _worked_out(data: dict) -> list[str]:
    """The ratios a headline tends to want, so the model copies them instead of
    working them out."""
    lines = []
    for v in data["variants"]:
        build = v["build"]
        active, wait = build["active_minutes"]["mean"], build["wait_minutes"]["mean"]
        if active and wait:
            lines.append(
                f"- {v['name']}: {active:.1f} active minutes a build, {active - wait:.1f} of them "
                f"the main agent working and {wait:.1f} waiting on subagents "
                f"({100 * wait / active:.0f}% waiting)."
            )
        elif active:
            lines.append(f"- {v['name']}: {active:.1f} active minutes a build, none of it waiting.")
    for a, b in combinations(data["variants"], 2):
        for key, what in (("cost_usd", "costs"), ("active_minutes", "takes")):
            one, other = a["build"][key]["mean"], b["build"][key]["mean"]
            if not one or not other or one == other:
                continue
            low, high = (a, b) if one < other else (b, a)
            low_value, high_value = sorted((one, other))
            more = "more" if key == "cost_usd" else "longer"
            unit = (lambda x: f"${x:.2f}") if key == "cost_usd" else (lambda x: f"{x:.1f} min")
            lines.append(
                f"- {high['name']} {what} {100 * (high_value / low_value - 1):.0f}% {more} "
                f"than {low['name']} ({unit(high_value)} against {unit(low_value)})."
            )
    for judge in _judge_spread(data):
        lines.append(f"- {judge}")
    return ["## Worked out for you\n", *lines, ""] if lines else []


def _judge_spread(data: dict) -> list[str]:
    """Each Judge's fewest and most critical and major Findings in one Trial."""
    found: dict[str, dict[str, list[int]]] = {}
    for a in data["agreement"]:
        for judge, counts in a["by_judge"].items():
            for severity in ("critical", "major"):
                found.setdefault(judge, {}).setdefault(severity, []).append(counts[severity])
    return [
        f"{judge} reported "
        + " and ".join(f"{_span(c)} {s}" for s, c in counts.items())
        + " Findings in one Trial."
        for judge, counts in found.items()
    ]


def _span(values: list[int]) -> str:
    return str(min(values)) if min(values) == max(values) else f"{min(values)}–{max(values)}"


def _subagents(data: dict) -> list[str]:
    names = sorted(
        {s["name"] for r in data["trials"] for s in (r.get("timeline") or {}).get("subagents", [])}
    )
    if not names:
        return []
    return ["## Subagents\n", "\n".join(f"- {n}" for n in names), ""]


def _plan(folder: Path) -> list[str]:
    plan = folder / "plan"
    if not plan.is_dir():
        return []
    parts, left = ["# The plan\n"], PLAN_LIMIT
    for f in sorted(p for p in plan.rglob("*") if p.is_file()):
        if left <= 0:
            parts.append("[the rest of the plan cut]")
            break
        text = _cut(f.read_text(errors="replace"), left)
        left -= len(text)
        parts.append(f"## {f.relative_to(plan).as_posix()}\n\n{text}\n")
    return parts


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[cut]"


# --- The reply -------------------------------------------------------------------


def check_words(value: dict, data: dict) -> dict[str, Any]:
    """The insights, checked against the fixed shape. What the page has no room
    for, and Defects or Variants the results do not have, are dropped."""
    variants = [v["name"] for v in data["variants"]]
    defects = {d["id"] for d in data["defects"] or []}
    headline = _line(value, "headline")
    if not headline:
        raise VerdictError("headline is missing")
    pick = value.get("pick")
    if pick is not None and pick not in variants:
        raise VerdictError(f"pick is {pick!r}, which is no Variant")
    return {
        "headline": headline,
        "stats": [
            {"value": _line(s, "value"), "label": _line(s, "label")}
            for s in _items(value, "stats", dict)
        ],
        "pick": pick,
        "pick_title": _line(value, "pick_title"),
        "task": _line(value, "task"),
        "variants": {k: v for k, v in _mapping(value, "variants").items() if k in variants},
        "sections": {k: v for k, v in _mapping(value, "sections").items() if k in SECTIONS},
        "defect_titles": {
            k: v for k, v in _mapping(value, "defect_titles").items() if k in defects
        },
        "roles": [
            {"name": _line(r, "name"), "subagents": _strings(r, "subagents")}
            for r in _items(value, "roles", dict)
        ],
        "caveats": [
            {"title": _line(c, "title"), "detail": _line(c, "detail")}
            for c in _items(value, "caveats", dict)
        ],
        "why": _items(value, "why", str),
        "next": _items(value, "next", str),
    }


def _line(value: dict, key: str) -> str:
    found = value.get(key)
    if found is None:
        return ""
    if isinstance(found, int | float) and not isinstance(found, bool):
        found = str(found)
    if not isinstance(found, str):
        raise VerdictError(f"{key} is not text")
    return " ".join(found.split())


def _items(value: dict, key: str, kind: type) -> list:
    found = value.get(key) or []
    if not isinstance(found, list) or not all(isinstance(x, kind) for x in found):
        raise VerdictError(f"{key} is not a list of {'text' if kind is str else 'objects'}")
    found = [" ".join(x.split()) for x in found] if kind is str else found
    return found[: MOST.get(key, len(found))]


def _strings(value: dict, key: str) -> list[str]:
    found = value.get(key) or []
    if not isinstance(found, list) or not all(isinstance(x, str) for x in found):
        raise VerdictError(f"{key} is not a list of text")
    return found


def _mapping(value: dict, key: str) -> dict[str, str]:
    """Text by name. A name left null is left out, so the page uses its own words."""
    found = value.get(key) or {}
    if not isinstance(found, dict):
        raise VerdictError(f"{key} is not a mapping of text")
    found = {k: v for k, v in found.items() if v is not None}
    if not all(isinstance(v, str) for v in found.values()):
        raise VerdictError(f"{key} is not a mapping of text")
    return {k: " ".join(v.split()) for k, v in found.items()}
