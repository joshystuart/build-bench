"""Line counts, plan criteria and checks."""

import json
from pathlib import Path

import pytest
from conftest import results_folder

from build_bench import settings as settings_module
from build_bench.lines import count_lines, criteria_ticked, matches

DIFFS = Path(__file__).parent / "fixtures" / "diffs"


def counts(diff: str, tests, ignore, rust=False) -> dict:
    found = count_lines((DIFFS / diff).read_text(), tests, ignore, rust)
    return {k: found[k] for k in ("prod_added", "prod_removed", "test_added", "test_removed")}


def preset_rules(experiment, language: str):
    return settings_module.path_rules(settings_module.load(experiment(languages=[language])))


def test_custom_globs_with_no_preset():
    assert counts("custom.diff", ["spec/**"], ["**/*.lock"]) == {
        "prod_added": 4,
        "prod_removed": 1,
        "test_added": 2,
        "test_removed": 0,
    }


@pytest.mark.parametrize(
    "language, expected",
    [
        ("rust", (1, 0, 3, 0)),
        ("typescript", (2, 1, 3, 0)),
        ("python", (5, 1, 3, 0)),
        ("go", (2, 0, 3, 0)),
    ],
)
def test_each_preset(experiment, language, expected):
    tests, ignore, rust = preset_rules(experiment, language)
    found = counts(f"{language}.diff", tests, ignore, rust)
    assert tuple(found.values()) == expected


def test_a_preset_file_beside_the_settings_is_used_without_code_changes(experiment):
    presets = experiment().parent / "presets"
    presets.mkdir()
    (presets / "ruby.yaml").write_text('test_paths: ["spec/**"]\nignore_paths: ["**/*.lock"]\n')
    tests, ignore, rust = preset_rules(experiment, "ruby")
    assert counts("custom.diff", tests, ignore, rust)["test_added"] == 2
    assert counts("custom.diff", tests, ignore, rust)["prod_added"] == 4


def test_presets_and_settings_globs_add_up(experiment):
    s = settings_module.load(experiment(languages=["go"], test_paths=["spec/**"]))
    tests, ignore, _ = settings_module.path_rules(s)
    assert "**/*_test.go" in tests and "spec/**" in tests and "**/go.sum" in ignore


def test_rust_inline_test_blocks_count_as_test_only_with_the_rust_preset(experiment):
    tests, ignore, rust = preset_rules(experiment, "rust")
    assert counts("rust-inline.diff", tests, ignore, rust) == {
        "prod_added": 4,
        "prod_removed": 0,
        "test_added": 5,
        "test_removed": 0,
    }
    assert counts("rust-inline.diff", [], [], False) == {
        "prod_added": 9,
        "prod_removed": 0,
        "test_added": 0,
        "test_removed": 0,
    }


@pytest.mark.parametrize(
    "path, pattern, hit",
    [
        ("tests/a/b.rs", "tests/**", True),
        ("crates/x/tests/a.rs", "**/tests/**", True),
        ("src/a.test.ts", "**/*.test.ts", True),
        ("a.test.ts", "**/*.test.ts", True),
        ("src/atest.ts", "**/*.test.ts", False),
        ("Cargo.lock", "Cargo.lock", True),
        ("x/Cargo.lock", "Cargo.lock", False),
    ],
)
def test_glob_matching(path, pattern, hit):
    assert matches(path, [pattern]) is hit


def test_plan_criteria_ticked_are_counted_from_the_plan_diff():
    assert criteria_ticked((DIFFS / "plan-two-ticks.diff").read_text()) == 2
    assert criteria_ticked("") == 0


# --- Through the command line --------------------------------------------------------


def test_checks_run_in_a_throwaway_copy(experiment, fake, cli):
    settings = experiment(
        checks="echo checked > checks-was-here.txt && rm calc.py && echo all good",
    )
    assert cli("run", settings)[0] == 0
    trial = results_folder(settings) / "trials" / "alpha-1"
    before = sorted(
        p.relative_to(trial / "workspace").as_posix() for p in trial.rglob("workspace/**/*")
    )
    assert cli("judge", settings)[0] == 0
    measured = json.loads((trial / "measurements.json").read_text())
    assert measured["checks"]["ran"] is True
    assert measured["checks"]["passed"] is True
    assert measured["checks"]["exit_code"] == 0
    assert (trial / "checks.log").read_text() == "all good\n"
    after = sorted(
        p.relative_to(trial / "workspace").as_posix() for p in trial.rglob("workspace/**/*")
    )
    assert before == after
    assert (trial / "workspace/repo/calc.py").exists()
    # The fake ticked one plan box.
    assert measured["criteria_ticked"] == 1
    assert measured["lines"]["prod_added"] == 2  # fake_committed.txt and fake_uncommitted.txt


def test_failing_checks_are_saved_as_failed(experiment, fake, cli):
    settings = experiment(checks="echo nope && exit 3")
    cli("run", settings)
    cli("judge", settings)
    measured = json.loads(
        (results_folder(settings) / "trials/alpha-1/measurements.json").read_text()
    )
    assert measured["checks"]["passed"] is False
    assert measured["checks"]["exit_code"] == 3


def test_no_checks_command_records_that_checks_did_not_run(experiment, fake, cli):
    settings = experiment()
    cli("run", settings)
    assert cli("judge", settings)[0] == 0
    measured = json.loads(
        (results_folder(settings) / "trials/alpha-1/measurements.json").read_text()
    )
    assert measured["checks"] == {"ran": False, "reason": "the settings name no checks command"}
