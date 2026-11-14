"""What a stand-in Judge replies, worked out from the prompt it was sent.

The three judging steps are told apart by the reply shape each prompt asks for.
"""

import json
import re

FOUND = """## Review
Mostly fine.

```json
{"findings": [
  {"title": "mul is missing", "severity": "major", "where": "calc.py:2",
   "detail": "The plan asks for mul.", "reproduced": true},
  {"title": "odd name", "severity": "nit"}],
 "note": "Fine."}
```
"""


def kind(prompt: str) -> str | None:
    """Which step a prompt belongs to: find, merge or check; None for a build."""
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


def check_reply(prompt: str, present: str = "yes") -> str:
    asked = re.findall(r"^### (D\d+):", prompt.split("## Known defects")[-1], flags=re.M)
    answers = [{"id": d, "present": present, "where": "calc.py:2", "evidence": "e"} for d in asked]
    return fenced({"answers": answers})
