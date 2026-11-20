"""Where and how `judge` runs the checks, driven through the command line."""

import json

from conftest import results_folder, workspace_home

#: `pre_build` records the folder it ran in, in a file git ignores, as a Docker
#: Compose project name or a mise trust would; the checks pass only in that folder.
RECORD_WHERE = "mkdir -p build && pwd > build/where"
SAME_FOLDER = 'test "$(cat build/where)" = "$(pwd)"'


def measured(settings, trial="alpha-1") -> dict:
    return json.loads(
        (results_folder(settings) / "trials" / trial / "measurements.json").read_text()
    )


def built(experiment, cli, **extra):
    settings = experiment(check_base=False, **extra)
    code, out, _ = cli("run", settings)
    assert code == 0, out
    return settings


def test_folder_dependent_checks_fail_in_a_copy_and_pass_in_place(experiment, fake, cli):
    settings = built(experiment, cli, pre_build=[RECORD_WHERE], checks=SAME_FOLDER)
    assert cli("judge", settings)[0] == 0
    assert measured(settings)["checks"]["passed"] is False

    settings = built(
        experiment, cli, pre_build=[RECORD_WHERE], checks=SAME_FOLDER, checks_in_place=True
    )
    assert cli("judge", settings)[0] == 0
    assert measured(settings)["checks"]["passed"] is True


def test_in_place_checks_leave_the_workspace_as_the_build_left_it(experiment, fake, cli):
    messy = (
        "echo changed by the checks > calc.py && echo new > added-by-checks.txt"
        " && rm fake_uncommitted.txt && mkdir -p build && echo out > build/output.txt"
    )
    settings = built(experiment, cli, checks=messy, checks_in_place=True)
    repo = workspace_home(results_folder(settings) / "trials" / "alpha-1") / "repo"
    calc = (repo / "calc.py").read_text()

    assert cli("judge", settings)[0] == 0
    assert measured(settings)["checks"]["passed"] is True
    assert (repo / "calc.py").read_text() == calc
    assert (repo / "fake_uncommitted.txt").read_text() == "left uncommitted\n"
    assert not (repo / "added-by-checks.txt").exists()
    # Files git ignores are build output and caches: they stay as the checks left them.
    assert (repo / "build/output.txt").read_text() == "out\n"


def agent_judge(**extra):
    return {"name": "blind-agent", "type": "claude", "model": "claude-sonnet-5-5", **extra}


def test_a_workspace_that_cannot_be_put_back_is_not_judged(experiment, fake, cli):
    # A new file in a folder the checks made read-only cannot be removed again.
    stuck = "mkdir stuck && echo x > stuck/new.txt && chmod 555 stuck"
    settings = built(experiment, cli, checks=stuck, checks_in_place=True, judges=[agent_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert "[measure failed] alpha-1: the workspace could not be put back after the checks" in out
    assert "could not be put back" in measured(settings)["error"]
    assert fake.calls(judge=True) == []
    repo = workspace_home(results_folder(settings) / "trials" / "alpha-1") / "repo"
    (repo / "stuck").chmod(0o755)


def test_copy_setup_prepares_the_checks_copy_in_order(experiment, fake, cli):
    settings = built(
        experiment,
        cli,
        pre_build=[RECORD_WHERE],
        checks=f'{SAME_FOLDER} && test "$(cat order)" = "first second"',
        copy_setup=[
            # Told where the Trial's own workspace is, so it can reuse what that started.
            'test "$(cat "$BUILD_BENCH_WORKSPACE/build/where")" = "$BUILD_BENCH_WORKSPACE"'
            " && printf first > order",
            "printf ' second' >> order && pwd > build/where",
        ],
    )
    assert cli("judge", settings)[0] == 0
    assert measured(settings)["checks"]["passed"] is True
    repo = workspace_home(results_folder(settings) / "trials" / "alpha-1") / "repo"
    assert not (repo / "order").exists()


def test_a_failing_copy_setup_means_the_checks_were_not_run(experiment, fake, cli):
    settings = built(experiment, cli, checks="true", copy_setup=["echo trusting", "exit 4"])
    assert cli("judge", settings)[0] == 0
    checks = measured(settings)["checks"]
    assert checks["ran"] is False
    assert checks["reason"] == "copy_setup: `exit 4` exited with code 4"
    log = (results_folder(settings) / "trials/alpha-1/checks.log").read_text()
    assert "$ echo trusting\ntrusting\n$ exit 4\n" in log


TWO_VARIANTS = [
    {"name": "alpha", "prompt": "prompts/build.md", "model": "m"},
    {"name": "beta", "prompt": "prompts/build.md", "model": "m"},
]


def test_copy_setup_prepares_every_agent_judges_copy(experiment, fake, cli):
    settings = built(
        experiment,
        cli,
        variants=TWO_VARIANTS,
        copy_setup=['echo "$BUILD_BENCH_WORKSPACE" > copy-setup.txt'],
        judges=[agent_judge(compare={"max_pairs": 1})],
    )
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    trial = results_folder(settings) / "trials" / "alpha-1"
    for kind in ("find", "check"):
        calls = fake.calls(judge=True, kind=kind)
        assert len(calls) == 2
        for call in calls:
            assert "copy-setup.txt" in call["cwd_files"]
    [compared] = fake.calls(judge=True, kind="compare")
    assert {"build-1/copy-setup.txt", "build-2/copy-setup.txt"} <= set(compared["cwd_files"])
    [merged] = fake.calls(judge=True, kind="merge")
    assert merged["cwd_files"] == []
    assert not (workspace_home(trial) / "repo" / "copy-setup.txt").exists()


def test_a_failing_copy_setup_fails_the_judging_step_and_is_tried_again(experiment, fake, cli):
    settings = built(experiment, cli, copy_setup=["exit 4"], judges=[agent_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert "[verdict failed] alpha-1 by blind-agent: copy_setup: `exit 4` exited with code 4" in out
    verdict = results_folder(settings) / "trials/alpha-1/verdicts/blind-agent.json"
    assert json.loads(verdict.read_text())["reason"] == "copy_setup: `exit 4` exited with code 4"
    assert fake.calls(judge=True) == []

    settings.write_text(settings.read_text().replace("exit 4", "echo fixed"))
    code, out, err = cli("judge", settings)
    assert code == 0, out + err
    assert json.loads(verdict.read_text())["status"] == "ok"
