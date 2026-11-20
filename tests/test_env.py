"""The Experiment's environment (`env`) and its runtime check, through the command line."""

import json
import os

from conftest import API_KEY, results_folder, workspace_home

PROBE = 'echo "probe=$BENCH_PROBE"'


def saved(settings, *path):
    return json.loads(results_folder(settings).joinpath(*path).read_text())


def text(settings, *path):
    return results_folder(settings).joinpath(*path).read_text()


def test_env_reaches_every_command_and_agent(experiment, fake, cli, monkeypatch):
    monkeypatch.setenv("PROBE_PART", "from-the-shell")
    settings = experiment(
        env={"BENCH_PROBE": "pinned-${PROBE_PART}"},
        variants=[
            {"name": "alpha", "prompt": "prompts/build.md", "model": "m"},
            {"name": "beta", "prompt": "prompts/build.md", "model": "m"},
        ],
        pre_build=[f"{PROBE} > build-probe.txt"],
        runtime_check=f'{PROBE} >&2; test "$BENCH_PROBE" = pinned-from-the-shell',
        copy_setup=[f"{PROBE} > copy-probe.txt"],
        checks=f"{PROBE} && cat copy-probe.txt",
        check_base=False,
        judges=[{"name": "cop", "type": "copilot", "model": "gpt", "compare": {"max_pairs": 1}}],
    )
    code, out, err = cli("run", settings)
    assert code == 0, out + err
    [build, _] = fake.calls()
    assert build["env"]["BENCH_PROBE"] == "pinned-from-the-shell"
    repo = workspace_home(results_folder(settings) / "trials" / "alpha-1") / "repo"
    assert (repo / "build-probe.txt").read_text() == "probe=pinned-from-the-shell\n"

    code, out, err = cli("judge", settings)
    assert code == 0, out + err
    log = text(settings, "trials", "alpha-1", "checks.log")
    # Printed by runtime_check, by the checks, and from the file copy_setup wrote.
    assert log.count("probe=pinned-from-the-shell") == 3
    judged = fake.calls(judge=True, agent="copilot")
    assert {c["kind"] for c in judged} == {"find", "merge", "check", "compare"}
    for call in judged:
        assert call["env"]["BENCH_PROBE"] == "pinned-from-the-shell"
    # build-bench's own environment is left as it was.
    assert "BENCH_PROBE" not in os.environ


def test_an_unset_variable_in_env_is_a_settings_error(experiment, fake, cli, monkeypatch):
    monkeypatch.delenv("NO_SUCH_VARIABLE", raising=False)
    settings = experiment(env={"PATH": "$HOME/bin:$NO_SUCH_VARIABLE", "OK": "${HOME}"})
    code, _, err = cli("run", settings)
    assert code == 2
    assert "env.PATH: $NO_SUCH_VARIABLE is not set" in err
    assert "env.OK" not in err
    assert fake.calls(judge=None) == []


def test_a_path_in_env_decides_which_agent_runs(experiment, fake, cli, monkeypatch, tmp_path):
    pinned = tmp_path / "pinned-bin"
    (tmp_path / "bin").rename(pinned)
    shell_path = os.environ["PATH"].replace(f"{tmp_path / 'bin'}{os.pathsep}", "")
    monkeypatch.setenv("PATH", shell_path)
    fake.set(version="2.1.400")
    settings = experiment(env={"PATH": f"{pinned}:${{PATH}}"}, claude_version="2.1.400")
    code, out, err = cli("run", settings)
    assert code == 0, out + err
    [build] = fake.calls()
    assert build["env"]["PATH"].startswith(f"{pinned}:")
    assert saved(settings, "experiment.json")["claude_version"] == "2.1.400"


def test_env_never_replaces_the_agents_login_or_config_folder(experiment, fake, cli):
    settings = experiment(
        env={
            "ANTHROPIC_API_KEY": "sk-from-env-not-used",
            "CLAUDE_CONFIG_DIR": "/elsewhere",
            "COPILOT_HOME": "/elsewhere",
        },
        variants=[
            {"name": "alpha", "prompt": "prompts/build.md", "model": "m"},
            {"name": "beta", "agent": "copilot", "prompt": "prompts/build.md", "model": "m"},
        ],
    )
    code, out, err = cli("run", settings)
    assert code == 0, out + err
    [claude] = fake.calls()
    assert claude["env"]["ANTHROPIC_API_KEY"] == API_KEY
    assert claude["env"]["CLAUDE_CONFIG_DIR"].endswith("/alpha-1/config")
    [copilot] = fake.calls(agent="copilot")
    assert copilot["env"]["COPILOT_HOME"].endswith("/beta-1/config")


WRONG_RUNTIME = "echo checking node >&2; echo 'expected v24, found v26' >&2; exit 1"


def test_a_wrong_runtime_fails_the_trial_before_pre_build(experiment, fake, cli):
    settings = experiment(runtime_check=WRONG_RUNTIME, pre_build=["touch prepared"])
    code, out, _ = cli("run", settings)
    assert code == 1
    reason = (
        f"runtime_check: `{WRONG_RUNTIME}` exited with code 1: "
        "checking node | expected v24, found v26"
    )
    assert saved(settings, "trials", "alpha-1", "status.json")["reason"] == reason
    assert f"[fail]  alpha-1 {reason}" in out
    assert fake.calls(judge=None) == []
    repo = workspace_home(results_folder(settings) / "trials" / "alpha-1") / "repo"
    assert not (repo / "prepared").exists()


def test_a_wrong_runtime_stops_the_checks_on_the_base_commit(experiment, fake, cli):
    settings = experiment(runtime_check=WRONG_RUNTIME, checks="true")
    code, _, err = cli("run", settings)
    assert code == 2
    assert "expected v24, found v26 on the base commit, so no Trial could pass them" in err
    assert fake.calls(judge=None) == []


def test_a_wrong_runtime_at_judge_time_means_the_checks_were_not_run(experiment, fake, cli):
    settings = experiment(
        checks="echo checked",
        judges=[{"name": "blind-agent", "type": "claude", "model": "m"}],
    )
    assert cli("run", settings)[0] == 0
    settings.write_text(settings.read_text() + f"runtime_check: {json.dumps(WRONG_RUNTIME)}\n")
    code, out, err = cli("judge", settings)
    assert code == 0, out + err
    checks = saved(settings, "trials", "alpha-1", "measurements.json")["checks"]
    assert checks["ran"] is False
    assert checks["reason"].endswith("expected v24, found v26")
    assert len(fake.calls(judge=True, kind="find")) == 1
