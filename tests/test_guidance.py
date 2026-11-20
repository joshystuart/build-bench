"""Guidance added to the built-in judging prompts (`judge_guidance`)."""

import hashlib
import json
from importlib import resources

from conftest import results_folder
from test_judges import agent_judge, api_judge, stub  # noqa: F401

TWO = [
    {"name": name, "prompt": "prompts/build.md", "model": "m", "repeats": 1}
    for name in ("alpha", "beta")
]
GUIDANCE = "Treat a missing migration as major. Ignore the README.\n"
HEADING = "## Guidance for this Experiment"
SHIPPED = {
    "find": "Your job is to find what is wrong with this build.",
    "merge": "Group the new findings below into distinct defects.",
    "check": "Do not report new problems; answer only for the defects listed.",
    "compare": "Decide which build you would rather ship and then maintain.",
}


def built(experiment, cli, guidance: str | None = GUIDANCE, **extra):
    folder = experiment().parent
    if guidance is not None:
        (folder / "guidance.md").write_text(guidance)
    settings = experiment(
        variants=TWO,
        checks="echo ok",
        judge_guidance="guidance.md" if guidance is not None else None,
        **extra,
    )
    assert cli("run", settings)[0] == 0
    return settings


def test_guidance_reaches_every_step_of_both_kinds_of_judge(experiment, fake, cli, stub):  # noqa: F811
    settings = built(
        experiment,
        cli,
        judges=[
            agent_judge(compare={"max_pairs": 1}),
            api_judge(compare={"max_pairs": 1}),
        ],
        merge_judge="blind-agent",
    )
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    for kind, shipped in SHIPPED.items():
        agent_prompts = [c["prompt"] for c in fake.calls(judge=True, kind=kind)]
        assert agent_prompts, kind
        for prompt in agent_prompts:
            assert shipped in prompt and f"{HEADING}\n\n{GUIDANCE}" in prompt
        if kind == "merge":
            continue
        for request in stub.sent(kind):
            system = request["messages"][0]["content"]
            assert shipped in system and f"{HEADING}\n\n{GUIDANCE}" in system


def test_an_api_judge_merges_with_the_guidance(experiment, fake, cli, stub):  # noqa: F811
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    [merge] = stub.sent("merge")
    assert HEADING in merge["messages"][0]["content"]


def test_a_judges_own_prompt_gets_the_guidance_too(experiment, fake, cli):
    folder = experiment().parent
    (folder / "mine.md").write_text('My own rules. Reply with "findings".\n')
    settings = built(experiment, cli, judges=[agent_judge(prompt="mine.md")])
    cli("judge", settings)
    prompt = fake.calls(judge=True, kind="find")[0]["prompt"]
    assert prompt.startswith("My own rules.")
    assert f"{HEADING}\n\n{GUIDANCE}" in prompt


def test_changed_guidance_judges_again_and_unchanged_asks_nothing(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge(compare={"max_pairs": 1})])
    assert cli("judge", settings)[0] == 0
    asked = len(fake.calls(judge=True))
    assert cli("judge", settings)[0] == 0
    assert len(fake.calls(judge=True)) == asked

    (settings.parent / "guidance.md").write_text("Be stricter about tests.\n")
    assert cli("judge", settings)[0] == 0
    again = fake.calls(judge=True)[asked:]
    # Two Verdicts, two Defect checks and one Comparison; the Defects are already merged.
    assert sorted(c["kind"] for c in again) == ["check", "check", "compare", "find", "find"]


def test_results_judged_without_guidance_keep_todays_prompt_hash(experiment, fake, cli):
    settings = built(experiment, cli, guidance=None, judges=[agent_judge()])
    assert cli("judge", settings)[0] == 0
    shipped = (resources.files("build_bench") / "prompts" / "judge.md").read_text()
    saved = json.loads(
        (results_folder(settings) / "trials/alpha-1/verdicts/blind-agent.json").read_text()
    )
    assert saved["prompt_sha"] == hashlib.sha256(shipped.encode()).hexdigest()[:12]


def test_a_missing_guidance_file_is_a_settings_error(experiment, cli):
    settings = experiment(judge_guidance="nowhere.md")
    code, _, err = cli("run", settings)
    assert code == 2
    assert "judge_guidance: file not found" in err
