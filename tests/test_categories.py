"""Categories: what kind of problem each Finding and Defect is."""

import json

import replies
from test_judges import api_judge, built, stub, verdict  # noqa: F401
from test_judges import saved as saved_file
from test_report import checked, no_models, saved, trial, write  # noqa: F401


def finding(title: str, category: str | None, severity: str = "minor") -> dict:
    found = {"title": title, "severity": severity, "where": "calc.py:1", "detail": f"{title}!"}
    return {**found, "category": category} if category else found


def findings(*found: dict) -> str:
    return replies.fenced({"findings": list(found), "note": ""})


# --- Judging ------------------------------------------------------------------------------


def test_the_find_prompt_defines_the_categories(experiment, fake, cli, stub):  # noqa: F811
    settings = built(experiment, cli, judges=[api_judge()])
    cli("judge", settings)
    system = stub.sent("find")[0]["messages"][0]["content"]
    for expected in (
        "- **behaviour**:",
        "- **test-gap**:",
        "- **scope**:",
        '"category": "behaviour"',
    ):
        assert expected in system


def test_a_verdict_without_a_known_category_fails(experiment, fake, cli, stub):  # noqa: F811
    stub.reply = findings(finding("no kind", None), finding("odd kind", "style"))
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 1
    reason = verdict(settings, "secretvariant-1", "diff-only")["reason"]
    assert "finding 1 has category None, not one of behaviour, test-gap, scope" in reason
    assert "finding 2 has category 'style'" in reason


def test_the_merge_sees_each_category_and_the_defect_keeps_the_one_named(
    experiment,
    fake,
    cli,
    stub,  # noqa: F811
):
    stub.reply = findings(finding("no test for mul", "test-gap"))
    stub.merge_reply = replies.fenced(
        {
            "defects": [
                {
                    "id": "new",
                    "title": "mul is untested",
                    "description": "d",
                    "category": "test-gap",
                    "findings": ["F1", "F2"],
                }
            ]
        }
    )
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    asked = stub.sent("merge")[0]["messages"][1]["content"]
    assert "### F1: no test for mul\n\nCategory: test-gap. Where: calc.py:1" in asked
    [defect] = saved_file(settings, "defects.json")["defects"]
    assert defect["category"] == "test-gap"


def test_a_defect_given_no_category_takes_its_findings_most_common_one(
    experiment,
    fake,
    cli,
    stub,  # noqa: F811
):
    # Each Trial's Verdict: one scope Finding. The reply names no category.
    stub.reply = findings(finding("plan names a missing table", "scope"))
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    [defect] = saved_file(settings, "defects.json")["defects"]
    assert defect["category"] == "scope"


def test_findings_of_different_categories_are_never_one_defect(
    experiment,
    fake,
    cli,
    stub,  # noqa: F811
):
    stub.reply = lambda n: findings(
        finding("mul is wrong", "behaviour") if n == 1 else finding("mul is untested", "test-gap")
    )
    stub.merge_reply = replies.fenced(
        {"defects": [{"id": "new", "title": "mul", "description": "d", "findings": ["F1", "F2"]}]}
    )
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert "defect 1 mixes findings of different categories: behaviour, test-gap" in out
    assert saved_file(settings, "defects.json")["status"] == "failed"


# --- The report ---------------------------------------------------------------------------


def categorised(experiment, defects: list[dict], answers: dict[str, list[dict]]):
    settings = experiment(
        variants=[
            {"name": "v1", "prompt": "prompts/build.md", "model": "m1", "repeats": 4},
            {"name": "v2", "prompt": "prompts/build.md", "model": "m2", "repeats": 4},
        ],
        judges=[{"name": "j", "type": "api", "model": "openai/gpt-5"}],
    )
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo"})
    write(folder / "defects.json", {"judge": "j", "status": "ok", "defects": defects})
    for variant, per_trial in answers.items():
        for n, answer in enumerate(per_trial, 1):
            trial(folder, f"{variant}-{n}", defect_checks={"j": checked(**answer)})
    return settings, folder


def defect(id: str, severity: str, category: str | None) -> dict:
    found = {
        "id": id,
        "title": f"problem {id}",
        "description": "d",
        "severity": severity,
        "findings": [f"v1-1/j/{id}"],
    }
    return {**found, "category": category} if category else found


THREE_KINDS = [
    defect("D1", "critical", "test-gap"),
    defect("D2", "minor", "behaviour"),
    defect("D3", "major", "scope"),
    defect("D4", "critical", "behaviour"),
]


def test_the_report_counts_defects_per_category_behaviour_first(experiment, cli, no_models):  # noqa: F811
    yes = {"D1": "yes", "D2": "yes", "D3": "yes", "D4": "no"}
    no_gap = {"D1": "no", "D2": "yes", "D3": "yes", "D4": "no"}
    settings, folder = categorised(experiment, THREE_KINDS, {"v1": [yes] * 4, "v2": [no_gap] * 4})
    assert cli("report", settings)[0] == 0
    data = json.loads((folder / "report.json").read_text())
    v1, v2 = data["variants"]
    assert v1["quality"]["categories"] == {
        "behaviour": {"mean": 1.0, "min": 1, "max": 1, "n": 4},
        "test-gap": {"mean": 1.0, "min": 1, "max": 1, "n": 4},
        "scope": {"mean": 1.0, "min": 1, "max": 1, "n": 4},
    }
    assert v2["quality"]["categories"]["test-gap"]["mean"] == 0.0
    # Behaviour first, then by severity within a category.
    assert [(d["id"], d["category"]) for d in data["defects"]] == [
        ("D4", "behaviour"),
        ("D2", "behaviour"),
        ("D1", "test-gap"),
        ("D3", "scope"),
    ]
    md = (folder / "report.md").read_text()
    assert "| Variant | Finished | Behaviour | Test gaps | Scope |" in md
    assert "| v1 | 4/4 | 1 | 1 | 1 |" in md
    assert "| v2 | 4/4 | 1 | 0 | 1 |" in md
    assert "| Defect | Category | Severity | What is wrong | Found by | v1 | v2 |" in md
    assert "| D1 | test-gap | critical | problem D1 | j | 4/4 | 0/4 |" in md
    page = (folder / "report.html").read_text()
    assert "<th scope='col'>Test gaps</th>" in page and "<td>test-gap</td>" in page


def test_only_behaviour_defects_are_weighed(experiment, cli, no_models):  # noqa: F811
    # The Variants differ only in a test gap: v1's Trials all have it, v2's none.
    gap = {"D1": "yes", "D2": "yes", "D3": "no", "D4": "no"}
    none = {"D1": "no", "D2": "yes", "D3": "no", "D4": "no"}
    settings, folder = categorised(experiment, THREE_KINDS, {"v1": [gap] * 4, "v2": [none] * 4})
    assert cli("report", settings)[0] == 0
    data = json.loads((folder / "report.json").read_text())
    [difference] = data["differences"]
    assert difference["measures"]["defects"]["lower"] is None
    md = (folder / "report.md").read_text()
    assert "| Variants | Behaviour defects per Trial | Cost ($) | Time (min) |" in md
    assert "| v1 and v2 | the same |" in md
    assert "| v1 | 4/4 | 1 | 1 | 0 |" in md and "| v2 | 4/4 | 1 | 0 | 0 |" in md


def test_defects_saved_before_categories_are_uncategorised(saved, cli, no_models):  # noqa: F811
    settings, folder = saved
    assert cli("report", settings)[0] == 0
    data = json.loads((folder / "report.json").read_text())
    assert {d["category"] for d in data["defects"]} == {"uncategorised"}
    md = (folder / "report.md").read_text()
    # No category column and no category table when no Defect has one.
    assert "| Defect | Severity | What is wrong | Found by | v1 | v2 |" in md
    assert "Test gaps" not in md
    # Defects without a category are still weighed, as they were before categories.
    assert data["differences"][0]["measures"]["defects"] is not None
