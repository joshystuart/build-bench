"""The compare step of `judge`: which of two Trials a Judge would rather ship."""

import json

from conftest import results_folder
from test_judges import agent_judge, api_judge, stub  # noqa: F401

TWO = [
    {"name": name, "prompt": "prompts/build.md", "model": "claude-opus-5-5", "repeats": 2}
    for name in ("secret-a", "secret-b")
]


def built(experiment, cli, **extra):
    settings = experiment(variants=TWO, checks="echo checks ran ok", **extra)
    assert cli("run", settings)[0] == 0
    return settings


def comparisons(settings, judge: str) -> dict[str, dict]:
    folder = results_folder(settings) / "comparisons" / judge
    return {p.stem: json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))}


def test_no_judge_compares_unless_asked(experiment, fake, cli, stub):  # noqa: F811
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    assert stub.sent("compare") == []
    assert not (results_folder(settings) / "comparisons").exists()


def test_an_api_judge_compares_two_blind_builds(experiment, fake, cli, stub):  # noqa: F811
    settings = built(experiment, cli, judges=[api_judge(compare={"max_pairs": 3})])
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert "[compared] secret-a-1 with secret-b-1 by diff-only" in out
    sent = stub.sent("compare")
    assert len(sent) == 3
    assert "which build you would rather ship" in sent[0]["messages"][0]["content"]
    text = sent[0]["messages"][1]["content"]
    assert text.count("- [ ] add mul") == 1  # the plan, once
    assert "# Build 1\n" in text and "# Build 2\n" in text
    assert text.count("+committed by the fake agent") == 2  # each build's diff
    assert "secret" not in text.lower() and "opus" not in text.lower()

    saved = comparisons(settings, "diff-only")
    # Each Trial is used once before any is used twice, and which Variant is
    # shown first alternates.
    assert [(c["first"], c["second"]) for c in saved.values()] == [
        ("secret-a-1", "secret-b-1"),
        ("secret-a-1", "secret-b-2"),
        ("secret-b-2", "secret-a-2"),
    ]
    # The stand-in always prefers build 1.
    assert [c["better"] for c in saved.values()] == ["secret-a-1", "secret-a-1", "secret-b-2"]
    first = saved["secret-a-1__secret-b-1"]
    assert (first["status"], first["why"]) == ("ok", "Build 1 handles failures.")
    assert first["cost_usd"] == 0.0042


def test_comparisons_already_saved_are_kept(experiment, fake, cli, stub):  # noqa: F811
    settings = built(experiment, cli, judges=[api_judge(compare={"max_pairs": 2})])
    assert cli("judge", settings)[0] == 0
    stub.requests.clear()
    # More pairs: only the new ones are asked about.
    settings = experiment(
        variants=TWO, checks="echo checks ran ok", judges=[api_judge(compare={"max_pairs": 4})]
    )
    assert cli("judge", settings)[0] == 0
    assert len(stub.sent("compare")) == 2
    assert len(comparisons(settings, "diff-only")) == 4


def test_same_and_bad_answers(experiment, fake, cli, stub):  # noqa: F811
    stub.better = "same"
    settings = built(experiment, cli, judges=[api_judge(compare={"max_pairs": 1})])
    assert cli("judge", settings)[0] == 0
    (only,) = comparisons(settings, "diff-only").values()
    assert (only["status"], only["better"]) == ("ok", None)

    stub.better = "3"
    settings = built(experiment, cli, judges=[api_judge("another", compare={"max_pairs": 1})])
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert "[compare failed] secret-a-1 with secret-b-1 by another: better is '3'" in out
    # The report lists it, and it is asked again next time.
    cli("report", settings)
    assert "(comparison): better is '3'" in (results_folder(settings) / "report.md").read_text()
    stub.better = "2"
    assert cli("judge", settings)[0] == 0
    (only,) = comparisons(settings, "another").values()
    assert only["better"] == "secret-b-1"


def test_an_agent_judge_compares_copies_of_both_builds(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge(compare={"max_pairs": 1})])
    assert cli("clean", settings)[0] == 0  # a rebuilt copy works too
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    (call,) = fake.calls(judge=True, kind="compare")
    assert {"build-1", "build-2"} <= set(call["cwd_files"])
    assert "`build-1/` and `build-2/`" in call["prompt"]
    assert "files git ignores" in call["prompt"]
    assert "secret" not in call["prompt"].lower()
    (only,) = comparisons(settings, "blind-agent").values()
    assert (only["first"], only["better"]) == ("secret-a-1", "secret-a-1")


def test_the_report_counts_the_comparisons(experiment, fake, cli, stub):  # noqa: F811
    settings = built(experiment, cli, judges=[api_judge(compare={"max_pairs": 4})])
    assert cli("judge", settings)[0] == 0
    cli("report", settings)
    md = (results_folder(settings) / "report.md").read_text()
    # Build 1 always wins and the Variants take turns being it.
    assert "| secret-a and secret-b | 4 | 2 | 2 | 0 | even |" in md
