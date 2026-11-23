"""What a stand-in Judge replies, worked out from the prompt it was sent.

The judging steps are told apart by the reply shape each prompt asks for.
"""

import json
import re

FOUND = """## Review
Mostly fine.

```json
{"findings": [
  {"title": "mul is missing", "severity": "major", "category": "behaviour",
   "where": "calc.py:2", "detail": "The plan asks for mul.", "reproduced": true},
  {"title": "odd name", "severity": "nit", "category": "behaviour"}],
 "note": "Fine."}
```
"""


def kind(prompt: str) -> str | None:
    """Which step a prompt belongs to: find, merge, check, compare or insights; None
    for a build."""
    if '"headline"' in prompt:
        return "insights"
    if '"better"' in prompt:
        return "compare"
    if '"answers"' in prompt:
        return "check"
    if '"defects"' in prompt:
        return "merge"
    return "find" if '"findings"' in prompt else None


def fenced(value: dict) -> str:
    return f"```json\n{json.dumps(value)}\n```"


def merge_reply(prompt: str) -> str:
    """Every new Finding goes into the first existing Defect, or else into one new one."""
    existing = re.findall(r"^### (D\d+):", prompt, flags=re.M)
    findings = re.findall(r"^### (F\d+):", prompt, flags=re.M)
    if existing:
        return fenced({"defects": [{"id": existing[0], "findings": findings}]})
    new = {"id": "new", "title": "mul is missing", "description": "Look for mul in calc.py."}
    return fenced({"defects": [{**new, "findings": findings}]})


def check_reply(prompt: str, present: str = "yes", in_base: str = "no") -> str:
    asked = re.findall(r"^### (D\d+):", prompt.split("## Known defects")[-1], flags=re.M)
    answer = {"present": present, "where": "calc.py:2", "evidence": "e"}
    if present == "yes":
        answer["severity"] = "minor"
        answer["in_base"] = in_base
    return fenced({"answers": [{"id": d, **answer} for d in asked]})


def compare_reply(better: str = "1") -> str:
    return fenced({"better": better, "reason": "Build 1 handles failures."})


def insights_reply(prompt: str) -> str:
    """Words for insights.html: the first Variant named in the prompt is the pick, and
    every subagent named in it builds."""
    variants = re.findall(r"^### (\S+)$", prompt, flags=re.M)
    subagents = re.findall(r"^- (.+)$", prompt.split("## Subagents")[-1], flags=re.M)
    defects = re.findall(r"^\| (D\d+) \|", prompt, flags=re.M)
    return fenced(
        {
            "headline": "Same quality. *v1* costs less.",
            "stats": [{"value": "+50%", "label": "cost of v2 over v1"}],
            "pick": variants[0] if variants else None,
            "pick_title": "The cheaper one",
            "task": "Add mul",
            "variants": {v: f"What {v} does" for v in variants},
            "sections": {"quality": "No difference in *quality*", "cost": "v2 costs more"},
            "defect_titles": {d: f"Plain {d}" for d in defects},
            "roles": [{"name": "Explore", "subagents": subagents}] if subagents else [],
            "caveats": [{"title": "Few runs.", "detail": "Read with care."}],
            "why": ["Same quality"],
            "next": ["Run more"],
        }
    )
