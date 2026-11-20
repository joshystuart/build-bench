"""Merging overlapping test gaps, and Defects the base commit already had."""

import json

import replies
from test_categories import categorised, defect
from test_judges import agent_judge, api_judge, built, stub  # noqa: F401
from test_judges import defect_check as saved_check
from test_judges import saved as saved_file
from test_report import no_models, saved, trial, write  # noqa: F401


def test_the_merge_prompt_keeps_apart_but_joins_test_gaps_one_test_closes(
    experiment,
    fake,
    cli,
    stub,  # noqa: F811
):
    gap = {"title": "mul untested", "severity": "minor", "category": "test-gap", "where": "t.py"}
    stub.reply = replies.fenced({"findings": [gap], "note": ""})
    stub.merge_reply = replies.fenced(
        {
            "defects": [
                {
                    "id": "new",
                    "title": "no test for mul",
                    "description": "d",
                    "category": "test-gap",
                    "findings": ["F1", "F2"],
                }
            ]
        }
    )
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    system = stub.sent("merge")[0]["messages"][0]["content"]
    assert "When in doubt, keep them apart." in system
    assert "Test-gap findings that one added test would close are the same defect" in system
    [merged] = saved_file(settings, "defects.json")["defects"]
    assert [k.split("/")[0] for k in merged["findings"]] == ["secretvariant-1", "secretvariant-2"]


def test_the_defect_check_asks_whether_a_defect_is_in_the_base(
    experiment,
    fake,
    cli,
    stub,  # noqa: F811
):
    stub.in_base = "yes"
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    system = stub.sent("check")[0]["messages"][0]["content"]
    assert '"in_base": "no"' in system
    answer = saved_check(settings, "secretvariant-1", "diff-only")["answers"]["D1"]
    assert answer["in_base"] == "yes"


def test_a_bad_in_base_answer_fails_the_check(experiment, fake, cli, stub):  # noqa: F811
    stub.in_base = "maybe"
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 1
    reason = saved_check(settings, "secretvariant-1", "diff-only")["reason"]
    assert "D1 has in_base 'maybe', not one of yes, no, unsure" in reason


def test_an_agent_judge_is_told_how_to_look_at_the_base(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    assert cli("judge", settings)[0] == 0
    [call, _] = fake.calls(judge=True, kind="check")
    assert "git stash --include-untracked" in call["prompt"]
    assert "git stash pop" in call["prompt"]
    # No second checkout: the copy is the build, with the base in its history.
    assert not any(f.startswith("base") for f in call["cwd_files"])


# --- The report ---------------------------------------------------------------------------


def answers(**given: tuple[str, str | None]) -> dict:
    """An ok Defect check: `answers(D1=("yes", "no"))` is present, not in the base."""
    return {
        "status": "ok",
        "answers": {
            d: {"present": p, "severity": "minor" if p == "yes" else None, "in_base": b}
            for d, (p, b) in given.items()
        },
    }


def in_base_report(experiment, cli, per_trial: list[str | None]):
    """Four Trials of each Variant have D1 (behaviour); v1's also have D2. The
    Judge answers whether D1 is in the base with `per_trial`, Trial by Trial."""
    settings, folder = categorised(
        experiment,
        [defect("D1", "major", "behaviour"), defect("D2", "minor", "behaviour")],
        {"v1": [{}] * 4, "v2": [{}] * 4},
    )
    for n, base in enumerate(per_trial):
        variant, repeat = ("v1", n + 1) if n < 4 else ("v2", n - 3)
        has_d2 = "yes" if variant == "v1" else "no"
        write(
            folder / "trials" / f"{variant}-{repeat}" / "defect-checks" / "j.json",
            answers(D1=("yes", base), D2=(has_d2, "no" if has_d2 == "yes" else None)),
        )
    assert cli("report", settings)[0] == 0
    return json.loads((folder / "report.json").read_text()), (folder / "report.md").read_text()


def test_a_defect_most_judges_place_in_the_base_is_set_apart(experiment, cli, no_models):  # noqa: F811
    data, md = in_base_report(experiment, cli, ["yes"] * 5 + ["no", "unsure", "unsure"])
    by_id = {d["id"]: d for d in data["defects"]}
    assert by_id["D1"]["in_base"] is True
    assert by_id["D1"]["in_base_answers"] == {"yes": 5, "no": 1, "unsure": 2}
    v1, v2 = data["variants"]
    # D1 counts for no Variant: v1 has only D2, v2 nothing.
    assert v1["quality"]["minor"]["mean"] == 1.0 and v2["quality"]["minor"]["mean"] == 0.0
    assert v2["quality"]["categories"]["behaviour"]["mean"] == 0.0
    [difference] = data["differences"]
    assert difference["measures"]["defects"]["by"] == 1.0
    table = md.split("## Defects already in the base commit")[1]
    # The Judges voted it minor where they found it.
    assert "| D1 | behaviour | minor | problem D1 | j | 4/4 | 4/4 |" in table
    assert "| D1 |" not in md.split("## Defects already in the base commit")[0]
    assert "no Variant caused them" in table


def test_a_tie_or_only_unsure_answers_is_not_in_the_base(experiment, cli, no_models):  # noqa: F811
    data, md = in_base_report(experiment, cli, ["yes"] * 4 + ["no"] * 4)
    assert {d["id"]: d["in_base"] for d in data["defects"]}["D1"] is False
    assert "## Defects already in the base commit" not in md

    data, _ = in_base_report(experiment, cli, ["unsure"] * 8)
    assert {d["id"]: d["in_base"] for d in data["defects"]}["D1"] is False


def test_the_html_lists_defects_in_the_base_apart(experiment, cli, no_models):  # noqa: F811
    in_base_report(experiment, cli, ["yes"] * 8)
    folder = experiment().parent / ".bench" / "results" / "demo" / "20260101-000000"
    page = (folder / "report.html").read_text()
    assert "Defects already in the base commit" in page


def test_defect_checks_saved_before_in_base_still_report(saved, cli, no_models):  # noqa: F811
    settings, folder = saved
    assert cli("report", settings)[0] == 0
    data = json.loads((folder / "report.json").read_text())
    assert not any(d["in_base"] for d in data["defects"])
