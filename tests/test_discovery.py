"""Who found each Defect, and who only checked for it."""

import json

from test_report import checked, no_models, ok, saved, trial, write  # noqa: F401


def discovery(experiment, cli):
    """D1 was found by Judge a in v1-1; v1-2 and v2-1 have it only by Defect checks.
    In v2-2 Judge a found it and Judge b only checked for it."""
    settings = experiment(
        variants=[
            {"name": "v1", "prompt": "prompts/build.md", "model": "m1", "repeats": 2},
            {"name": "v2", "prompt": "prompts/build.md", "model": "m2", "repeats": 2},
        ],
        judges=[{"name": n, "type": "api", "model": "openai/gpt-5"} for n in ("a", "b")],
    )
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo"})
    write(
        folder / "defects.json",
        {
            "judge": "a",
            "status": "ok",
            "defects": [
                {
                    "id": "D1",
                    "title": "mul is missing",
                    "description": "d",
                    "severity": "major",
                    "findings": ["v1-1/a/aaa", "v2-2/a/bbb"],
                }
            ],
        },
    )
    for name in ("v1-1", "v1-2", "v2-1", "v2-2"):
        both = {"a": checked(D1="yes"), "b": checked(D1="yes")}
        trial(folder, name, verdicts={"a": ok("major"), "b": ok()}, defect_checks=both)
    assert cli("report", settings)[0] == 0
    return folder


def test_each_trial_with_a_defect_is_found_or_checked_only(experiment, cli, no_models):  # noqa: F811
    folder = discovery(experiment, cli)
    data = json.loads((folder / "report.json").read_text())
    [d1] = data["defects"]
    assert d1["variants"]["v1"] == {
        "present": 2,
        "disputed": 0,
        "checked": 2,
        "found": 1,
        "checked_only": 1,
    }
    # v2-2: one Judge found it, the other only checked, so it counts as found.
    assert (d1["variants"]["v2"]["found"], d1["variants"]["v2"]["checked_only"]) == (1, 1)
    md = (folder / "report.md").read_text()
    assert "| D1 | major | mul is missing | a | 2/2 · found 1, checked only 1 |" in md
    assert "found 1, checked only 1" in (folder / "report.html").read_text()


def test_results_judged_earlier_show_discovery_too(saved, cli, no_models):  # noqa: F811
    settings, folder = saved
    assert cli("report", settings)[0] == 0
    data = json.loads((folder / "report.json").read_text())
    by_id = {d["id"]: d for d in data["defects"]}
    # D1's Findings came from v1-1 and v1-2, which both have it.
    assert (
        by_id["D1"]["variants"]["v1"]["found"],
        by_id["D1"]["variants"]["v1"]["checked_only"],
    ) == (2, 0)
    # D2 was found in v2-2, but disputed there, so no v2 Trial has it; v1-2 has it
    # only by a Defect check.
    assert by_id["D2"]["variants"]["v1"]["checked_only"] == 1
    assert by_id["D2"]["variants"]["v2"] == {
        "present": 0,
        "disputed": 1,
        "checked": 1,
        "found": 0,
        "checked_only": 0,
    }
