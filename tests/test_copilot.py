"""The GitHub Copilot CLI as the agent of a Variant and of a Judge, with a fake
`copilot` that replays a real recording of Copilot CLI 1.0.112."""

import json
from pathlib import Path

import pytest
from conftest import results_folder, workspace_home

from build_bench import progress
from build_bench.transcript import default_prices, measure

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "copilot"
GITHUB_TOKEN = "github_pat_test-secret-value-1234"


def copilot(name="pilot", **extra):
    return {
        "name": name,
        "agent": "copilot",
        "prompt": "prompts/build.md",
        "model": "claude-sonnet-5.5",
        **extra,
    }


CLAUDE = {"name": "alpha", "prompt": "prompts/build.md", "model": "claude-opus-5-5"}


def test_a_copilot_variant_builds_with_no_claude_login(experiment, fake, cli, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("COPILOT_MODEL", "some-other-model")
    monkeypatch.setenv("COPILOT_HOME", "/somewhere/else")
    exp = experiment().parent
    (exp / "skills" / "tdd").mkdir(parents=True)
    (exp / "skills" / "tdd" / "SKILL.md").write_text("Test first.\n")
    variant = copilot(effort="high", skills=["skills/tdd"])
    settings = experiment(auth=None, variants=[variant])
    code, out, err = cli("run", settings)
    assert code == 0, out + err

    [call] = fake.calls(agent="copilot")
    argv = call["argv"]
    assert argv[:2] == ["-p", "Build the plan in .scratch/plan. Tick each box.\n"]
    for flag in ("--allow-all", "--no-ask-user", "--no-auto-update"):
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5.5"
    assert argv[argv.index("--effort") + 1] == "high"
    trial = results_folder(settings) / "trials" / "pilot-1"
    home = workspace_home(trial)
    # Its own home folder, holding only the Variant's skills; nothing follows it in.
    assert call["env"]["COPILOT_HOME"] == str(home / "config")
    assert call["config_files"] == ["skills"]
    assert call["env"]["COPILOT_MODEL"] is None and call["env"]["ANTHROPIC_API_KEY"] is None
    assert call["cwd"] == str(home / "repo")
    assert fake.calls(judge=None) == []  # Claude Code was never started

    status = json.loads((trial / "status.json").read_text())
    assert (status["status"], status["agent"], status["agent_version"]) == (
        "finished",
        "copilot",
        "1.0.112",
    )
    assert status["claude_version"] is None
    assert (trial / "summary.md").read_text() == "Fake summary: all done. "
    assert "+committed by the fake agent" in (trial / "diff.patch").read_text()
    # The session's event log is the transcript, subagents and all.
    assert (trial / "transcript" / "main.jsonl").read_text() == (FIXTURE / "main.jsonl").read_text()
    experiment_file = json.loads((results_folder(settings) / "experiment.json").read_text())
    assert experiment_file["copilot_versions"] == ["1.0.112"]


def test_a_prompt_too_long_for_one_argument_goes_in_a_file(experiment, fake, cli):
    settings = experiment(auth=None, variants=[copilot()])
    long = "Build the plan in .scratch/plan.\n" + "x" * 200_000
    (settings.parent / "prompts" / "build.md").write_text(long)
    code, out, err = cli("run", settings)
    assert code == 0, out + err

    [call] = fake.calls(agent="copilot")
    home = workspace_home(results_folder(settings) / "trials" / "pilot-1")
    file = home / "config" / "prompt.md"
    assert call["argv"][1] == f"Read the file {file} in full and do what it says."
    assert file.read_text() == long


def test_a_real_copilot_run_is_measured_from_its_event_log():
    m = measure(FIXTURE, default_prices())
    # As Copilot itself totals them when the session ends (`session.shutdown`):
    # 13,002,580,000 nano AI units, at a cent an AI credit.
    assert m["cost_usd"] == pytest.approx(0.1300258)
    assert m["cost_basis"] == "Copilot AI credits"
    assert m["tokens"] == {
        "input": 20,
        "output": 1324,
        "cache_read": 121183,
        "cache_write": 41851,
        "total": 164378,
    }
    assert [(a["agent"], round(a["cost_usd"], 7)) for a in m["by_agent"]] == [
        ("main", 0.0630147),
        ("mul-test", 0.0670111),
    ]
    assert m["by_agent"][1]["tokens"]["output"] == 769
    assert m["unpriced_models"] == []
    # From the first event to the last; the main agent waited while its one subagent ran.
    assert m["total_seconds"] == 17.535
    [sub] = m["subagents"]
    assert (sub["name"], sub["type"], sub["background"]) == ("mul-test", "general-purpose", False)
    assert (sub["start"], sub["end"], sub["seconds"]) == (6.229, 15.707, 9.478)
    assert m["wait_seconds"] == 9.478 and m["wait_intervals"] == [[6.229, 15.707]]
    assert sorted(m["tool_seconds"]) == [
        "bash: cat",
        "bash: python",
        "bash: python3",
        "create",
        "edit",
    ]
    assert "task" not in m["tool_seconds"]


def test_a_copilot_trial_is_measured_and_reported_beside_a_claude_one(experiment, fake, cli):
    settings = experiment(variants=[CLAUDE, copilot()])
    assert cli("run", settings)[0] == 0
    assert cli("judge", settings)[0] == 0
    assert cli("report", settings)[0] == 0
    folder = results_folder(settings)
    measured = json.loads((folder / "trials/pilot-1/measurements.json").read_text())
    assert measured["cost_usd"] == pytest.approx(0.1300258)
    assert len(measured["subagents"]) == 1
    md = (folder / "report.md").read_text()
    assert "| pilot | 1/1 | 0.13 | 164378 | 0.29 | 0.16 | 1 |" in md
    assert "- Claude Code version: 2.1.319." in md
    assert "- Copilot CLI version: 1.0.112." in md
    assert "**Costs are on two bases.**" in md
    assert "mul-test" in (folder / "report.html").read_text()  # on the timeline


def test_a_copilot_failure_fails_the_trial_with_what_it_said(experiment, fake, cli):
    fake.set(copilot_mode="crash")
    settings = experiment(variants=[copilot()])
    code, out, _ = cli("run", settings)
    assert code == 1
    assert (
        "[fail]  pilot-1 error: exit code 1: Error: Access denied by policy settings "
        "(Request ID: 0000)"
    ) in out


def test_a_copilot_trial_stops_at_the_time_limit(experiment, fake, cli):
    fake.set(copilot_mode="hang")
    settings = experiment(variants=[copilot()], limits={"timeout_minutes": 0.01})
    code, out, _ = cli("run", settings)
    assert code == 1
    assert "[fail]  pilot-1 timeout: stopped after 0.01 minutes" in out


def test_the_copilot_version_can_be_pinned(experiment, fake, cli):
    code, _, err = cli("run", experiment(variants=[copilot()], copilot_version="9.9.9"))
    assert code == 2
    assert "Copilot CLI 1.0.112 is installed but the settings pin 9.9.9" in err
    assert fake.calls(agent="copilot") == []
    assert cli("run", experiment(variants=[copilot()], copilot_version="1.0.112"))[0] == 0


def test_a_github_token_reaches_copilot_and_is_never_saved(experiment, fake, cli, monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("GH_TOKEN", "gho_another-secret-value-5678")
    settings = experiment(variants=[copilot()])
    assert cli("run", settings)[0] == 0
    [call] = fake.calls(agent="copilot")
    assert call["env"]["COPILOT_GITHUB_TOKEN"] == GITHUB_TOKEN
    trial = results_folder(settings) / "trials" / "pilot-1"
    # The fake repeats GH_TOKEN in its final message.
    assert (trial / "summary.md").read_text() == "Fake summary: all done. [redacted]"
    for path in (p for p in trial.rglob("*") if p.is_file()):
        assert "secret-value" not in path.read_text(errors="replace"), path


def test_claude_code_needs_a_login_named_and_copilot_does_not(experiment, fake, cli):
    code, _, err = cli("run", experiment(auth=None, variants=[copilot(), CLAUDE]))
    assert code == 2
    assert "auth: required, because variants.1 is Claude Code" in err
    judge = {"name": "j", "type": "claude", "model": "claude-opus-5-5"}
    code, _, err = cli("run", experiment(auth=None, variants=[copilot()], judges=[judge]))
    assert code == 2
    assert "auth: required, because judges.0 is Claude Code" in err


def test_a_copilot_agent_judge(experiment, fake, cli):
    judge = {"name": "pilot-judge", "type": "copilot", "model": "gpt-6.1-sol", "effort": "high"}
    settings = experiment(variants=[CLAUDE], judges=[judge])
    assert cli("run", settings)[0] == 0
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    find, merge, check = fake.calls(judge=True, agent="copilot")
    assert (find["kind"], merge["kind"], check["kind"]) == ("find", "merge", "check")
    argv = find["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert "Your job is to find what is wrong" in argv[argv.index("-p") + 1]
    # A throwaway copy of the Trial, and an empty home folder of its own.
    trial = results_folder(settings) / "trials" / "alpha-1"
    assert not find["cwd"].startswith(str(workspace_home(trial)))
    assert find["config_files"] == []
    verdict = json.loads((trial / "verdicts" / "pilot-judge.json").read_text())
    assert verdict["status"] == "ok" and verdict["type"] == "copilot"
    assert [f["severity"] for f in verdict["findings"]] == ["major", "nit"]
    # What Copilot charged for the review, from its usage checkpoint.
    assert verdict["cost_usd"] == pytest.approx(0.1300258)
    checked = json.loads((trial / "defect-checks" / "pilot-judge.json").read_text())
    assert checked["answers"]["D1"]["present"] == "yes"


def test_the_live_display_reads_a_copilot_stream():
    found = progress.read_stream(FIXTURE / "stdout.jsonl", default_prices())
    # The main agent's three tool calls; its subagent's five are not its own progress.
    assert found.tools == 3
    assert found.last == "subagent: bash Run tests manually"
    assert found.subagents == {} and found.ended
    assert found.cost == pytest.approx(0.1300258)
