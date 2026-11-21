"""When `judge` runs the checks and the Judges again, and `judge --fresh`."""

import json

import pytest
from conftest import results_folder
from test_judges import agent_judge


def counting(runs) -> str:
    return f"echo ran >> {runs}"


def built(experiment, cli, runs, **extra):
    settings = experiment(
        checks=counting(runs),
        check_base=False,
        judges=[agent_judge(compare={"max_pairs": 1})],
        variants=[
            {"name": "alpha", "prompt": "prompts/build.md", "model": "m"},
            {"name": "beta", "prompt": "prompts/build.md", "model": "m"},
        ],
        **extra,
    )
    assert cli("run", settings)[0] == 0
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    return settings


def ran(runs) -> int:
    return len(runs.read_text().splitlines()) if runs.exists() else 0


def measured(settings, trial="alpha-1") -> dict:
    return json.loads(
        (results_folder(settings) / "trials" / trial / "measurements.json").read_text()
    )


def test_judging_again_with_nothing_changed_runs_and_asks_nothing(experiment, fake, cli, tmp_path):
    runs = tmp_path / "runs"
    settings = built(experiment, cli, runs)
    checks, asked = ran(runs), len(fake.calls(judge=True))
    assert (checks, asked) == (2, 6)  # two Verdicts, a merge, two Defect checks, a Comparison
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert (ran(runs), len(fake.calls(judge=True))) == (checks, asked)


@pytest.mark.parametrize(
    "change",
    [
        lambda text, runs: text.replace(f"echo ran >> {runs}", f"echo again >> {runs}"),
        lambda text, _: text + "env: {PINNED: pinned}\n",
        lambda text, _: text + "copy_setup: [echo set up]\n",
        lambda text, _: text + "runtime_check: echo fine\n",
        lambda text, _: text + "checks_in_place: true\n",
    ],
    ids=["checks", "env", "copy_setup", "runtime_check", "checks_in_place"],
)
def test_a_changed_checks_setting_reruns_only_the_checks(experiment, fake, cli, tmp_path, change):
    runs = tmp_path / "runs"
    settings = built(experiment, cli, runs)
    before, asked = measured(settings), len(fake.calls(judge=True))
    settings.write_text(change(settings.read_text(), runs))
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert "[checks again] alpha-1: the settings they ran under changed" in out
    assert ran(runs) == 4
    after = measured(settings)
    assert after["tokens"] == before["tokens"] and after["lines"] == before["lines"]
    assert len(fake.calls(judge=True)) == asked


def test_measurements_from_before_are_kept_while_the_settings_are_the_defaults(
    experiment, fake, cli, tmp_path
):
    runs = tmp_path / "runs"
    settings = built(experiment, cli, runs)
    for trial in ("alpha-1", "beta-1"):
        path = results_folder(settings) / "trials" / trial / "measurements.json"
        old = json.loads(path.read_text())
        del old["checks_key"]
        path.write_text(json.dumps(old))
    assert cli("judge", settings)[0] == 0
    assert ran(runs) == 2

    settings.write_text(settings.read_text() + "checks_in_place: true\n")
    assert cli("judge", settings)[0] == 0
    assert ran(runs) == 4


def test_fresh_judges_everything_again_from_nothing(experiment, fake, cli, tmp_path):
    runs = tmp_path / "runs"
    settings = built(experiment, cli, runs)
    folder = results_folder(settings)
    first = json.loads((folder / "defects.json").read_text())
    stray = folder / "trials" / "alpha-1" / "verdicts" / "gone.json"
    stray.write_text("{}")

    code, out, _ = cli("judge", settings, "--fresh")
    assert code == 0, out
    assert "Judging again from nothing" in out
    assert ran(runs) == 4
    again = fake.calls(judge=True)[6:]
    assert sorted(c["kind"] for c in again) == sorted(
        ["find", "find", "merge", "check", "check", "compare"]
    )
    defects = json.loads((folder / "defects.json").read_text())
    assert [d["id"] for d in defects["defects"]] == [d["id"] for d in first["defects"]] == ["D1"]
    assert defects["next_id"] == 2
    assert not stray.exists()


def test_checks_that_could_not_run_run_again_once_they_can(experiment, fake, cli, tmp_path):
    runs, ready = tmp_path / "runs", tmp_path / "ready"
    settings = experiment(checks=counting(runs), check_base=False, copy_setup=[f"test -f {ready}"])
    assert cli("run", settings)[0] == 0
    cli("judge", settings)
    assert ran(runs) == 0
    ready.touch()
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert "[checks again] alpha-1: they did not run last time" in out
    assert ran(runs) == 1 and measured(settings)["checks"]["ran"] is not False


def test_fresh_keeps_the_checks_of_a_removed_workspace(experiment, fake, cli, tmp_path):
    runs = tmp_path / "runs"
    settings = built(experiment, cli, runs)
    before = measured(settings)
    assert cli("clean", settings)[0] == 0
    code, out, _ = cli("judge", settings, "--fresh")
    assert code == 0, out
    assert measured(settings)["checks"] == before["checks"]
    assert len(fake.calls(judge=True)) == 12  # every Verdict and Defect check asked again


def test_all_does_not_take_fresh(experiment, fake, cli):
    settings = experiment()
    with pytest.raises(SystemExit) as stopped:
        cli("all", settings, "--fresh")
    assert stopped.value.code == 2
    assert fake.calls(judge=None) == []
