"""Measurements from transcripts, through `judge` and against saved fixtures."""

import json
from pathlib import Path

import pytest
from conftest import results_folder

from build_bench.settings import Price
from build_bench.transcript import command_kind, default_prices, measure

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


def test_tokens_and_cost_include_subagents_and_count_each_message_once():
    m = measure(FIXTURES / "subagents", default_prices())
    # Hand-computed from the ten distinct message ids across the three files
    # (the main transcript writes one message on three lines).
    assert m["tokens"] == {
        "input": 20,
        "output": 2700,
        "cache_read": 132978,
        "cache_write": 26625,
        "total": 162323,
    }
    # 20*0.10 + 2700*0.50 + 132978*0.01 + 16972*0.125 + 9653*0.20, per million.
    # Claude Code itself reported total_cost_usd 0.00673388 for this run.
    assert m["cost_usd"] == pytest.approx(0.00673388, abs=1e-10)
    assert [a["agent"] for a in m["by_agent"]] == [
        "main",
        "Count words in README and calc.py",
        "Run calc tests",
    ]
    assert sum(a["tokens"]["total"] for a in m["by_agent"]) == 162323
    assert m["unpriced_models"] == []


def test_prices_in_the_settings_override_the_defaults():
    prices = {**default_prices(), "claude-haiku-5-5": Price(input=1.0, output=5.0)}
    m = measure(FIXTURES / "subagents", prices)
    assert m["cost_usd"] == pytest.approx(0.0673388, abs=1e-9)


def test_an_unknown_model_is_listed_not_guessed():
    m = measure(FIXTURES / "subagents", {})
    assert m["cost_usd"] == 0
    assert m["unpriced_models"] == ["claude-haiku-5-5"]


def test_times_with_two_overlapping_subagents():
    m = measure(FIXTURES / "subagents", default_prices())
    # 09:00:21.292 to 09:00:35.474
    assert m["total_seconds"] == pytest.approx(14.182)
    subs = {s["name"]: s for s in m["subagents"]}
    assert subs["Count words in README and calc.py"]["seconds"] == pytest.approx(2.747)
    assert subs["Run calc tests"]["seconds"] == pytest.approx(4.094)
    # Waits 23.474-26.266 and 24.339-28.451 overlap: counted once, 23.474-28.451.
    assert m["wait_seconds"] == pytest.approx(4.977)
    assert m["wait_intervals"] == [[pytest.approx(2.182), pytest.approx(7.159)]]


def test_waiting_on_a_background_subagent():
    # Real Claude Code launched this kind of subagent in the background: the
    # Agent call returns at once, the main agent ends its turn at 3.0 s and is
    # woken at 6.0 s when the subagent (1.5 s to 5.5 s) finishes.
    m = measure(FIXTURES / "background", default_prices())
    assert m["subagents"][0]["background"] is True
    assert m["wait_intervals"] == [[1.0, 1.01], [3.0, 5.5]]
    assert m["wait_seconds"] == pytest.approx(2.51)


def test_tool_time_is_grouped_by_first_word_and_tool_name():
    m = measure(FIXTURES / "toolchains", default_prices())
    assert m["tool_seconds"] == {
        "Bash: cargo": pytest.approx(40.0),
        "Bash: git": pytest.approx(0.5),
        "Bash: npm": pytest.approx(7.5),
        "Read": pytest.approx(0.25),
    }


@pytest.mark.parametrize(
    "command, kind",
    [
        ("cd /x && cargo test", "cargo"),
        ("FOO=1 BAR=2 npm test", "npm"),
        ("/usr/local/bin/go test ./...", "go"),
        ("cd /x; pytest -q", "pytest"),
        ("cd /x", "cd"),
    ],
)
def test_command_kind(command, kind):
    assert command_kind(command) == kind


# --- Through the command line --------------------------------------------------------


def test_judge_measures_the_latest_results_folder_or_the_one_given(experiment, fake, cli):
    settings = experiment()
    assert cli("run", settings)[0] == 0
    first = results_folder(settings)
    assert cli("run", settings)[0] == 0
    second = sorted((settings.parent / ".bench" / "results" / "demo").iterdir())[-1]
    assert first != second

    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert f"Results: {second}" in out
    assert (second / "trials/alpha-1/measurements.json").is_file()
    assert not (first / "trials/alpha-1/measurements.json").exists()

    assert cli("judge", settings, "--results", first)[0] == 0
    measured = json.loads((first / "trials/alpha-1/measurements.json").read_text())
    assert measured["tokens"]["total"] == 162323


def test_a_failed_trial_is_skipped_and_a_measured_one_is_not_measured_again(experiment, fake, cli):
    fake.set(per_cwd={"/v-2/": "crash"})
    settings = experiment(
        variants=[{"name": "v", "prompt": "prompts/build.md", "model": "m", "repeats": 2}]
    )
    assert cli("run", settings)[0] == 1
    code, out, _ = cli("judge", settings)
    assert code == 0
    assert "[skip]  v-2: the Trial failed, so it is not measured" in out
    assert "[measured] v-1" in out
    folder = results_folder(settings)
    target = folder / "trials/v-1/measurements.json"
    before = target.stat().st_mtime_ns

    code, out, _ = cli("judge", settings)
    assert "[measured]" not in out
    assert target.stat().st_mtime_ns == before
    assert not (folder / "trials/v-2/measurements.json").exists()
