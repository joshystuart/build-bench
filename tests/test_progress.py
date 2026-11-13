"""The live display `run` draws while Trials run, when its output is a terminal."""

import sys

import pytest

from build_bench import progress

TODOS = {
    "type": "assistant",
    "message": {
        "id": "msg_1",
        "model": "claude-opus-5-5",
        "usage": {"input_tokens": 1000, "output_tokens": 50000},
        "content": [
            {
                "type": "tool_use",
                "name": "TodoWrite",
                "input": {
                    "todos": [
                        {"content": "add mul", "status": "completed"},
                        {"content": "add tests", "status": "in_progress"},
                        {"content": "update docs", "status": "pending"},
                    ]
                },
            }
        ],
    },
}
TESTS = {
    "type": "assistant",
    "message": {
        "id": "msg_2",
        "model": "claude-opus-5-5",
        "content": [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "uv run pytest -q", "description": "Run the tests"},
            }
        ],
    },
}


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch):
    """Make the command's output look like a terminal of a given size."""

    def size(lines: int = 40) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setenv("COLUMNS", "160")
        monkeypatch.setenv("LINES", str(lines))
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr(progress, "INTERVAL", 0.05)

    return size


def last_frame(out: str) -> list[str]:
    """What the screen holds when `run` ends: everything after the last redraw."""
    return out.rsplit("\x1b[J", 1)[1].splitlines()


def two_alphas(experiment, **extra):
    return experiment(
        variants=[{"name": "alpha", "prompt": "prompts/build.md", "model": "m", "repeats": 2}],
        **extra,
    )


def test_each_trial_shows_how_far_it_is_and_how_close_to_its_limits(
    experiment, fake, cli, terminal
):
    terminal()
    fake.set(sleep=0.6, events=[TODOS, TESTS])
    settings = two_alphas(experiment, limits={"timeout_minutes": 5, "max_budget_usd": 2})

    code, out, _ = cli("run", settings)

    assert code == 0, out
    # While alpha-1 runs: its own todo list says how far it is, beside its limits.
    assert "alpha-1  [██████████░░░░░░░░░░░░░░░░░░░░]  33%  todo list 1/3" in out
    assert "agent time [" in out and "/5m00s" in out
    assert "cost [█████░░░░░] $1.00/$2.00" in out  # 1k in, 50k out at $4/$20 per million
    assert "now: Bash Run the tests · " in out
    assert "waiting: alpha-2" in out
    # Before any Trial finishes only an upper bound is known; after, an estimate.
    assert "time left: unknown until a Trial finishes (timeouts allow agents up to 10m00s)" in out
    assert "time left: ~" in out and "(from 1 finished Trial(s))" in out
    # The lines `run` always prints still appear, above the display.
    assert "[done]  alpha-1 finished in" in out and "[done]  alpha-2 finished in" in out
    final = last_frame(out)
    assert "2/2 done · 0 running · 0 failed" in final[0]
    assert final[1].strip() == "all Trials ended"
    assert any(line.startswith("alpha-2  [█") and "done in" in line for line in final)


def test_without_a_todo_list_the_plan_or_finished_trials_say_how_far(
    experiment, fake, cli, terminal
):
    terminal()
    fake.set(sleep=0.6, events=[TESTS])
    settings = two_alphas(experiment)

    code, out, _ = cli("run", settings)

    assert code == 0, out
    # The fake ticks one of the plan's two boxes before it works.
    assert " 50%  plan 1/2 ticked" in out

    fake.set(sleep=0.6, events=[TESTS])
    code, out, _ = cli("run", experiment(plan=None, variants=[
        {"name": "alpha", "prompt": "prompts/plain.md", "model": "m", "repeats": 2}
    ]))  # fmt: skip
    assert code == 0, out
    assert "no estimate until a Trial finishes" in out
    # alpha-1 made one tool call, so alpha-2 at one call is as far as it can be shown.
    assert " 95%  1 of ~1 tool calls" in out


def test_a_trial_with_no_news_is_shown_as_quiet(experiment, fake, cli, terminal, monkeypatch):
    terminal()
    monkeypatch.setattr(progress, "STALL_SECONDS", 0)
    fake.set(sleep=0.4, events=[TESTS])
    code, out, _ = cli("run", experiment())
    assert code == 0, out
    assert "now: Bash Run the tests · quiet for 0s" in out


def test_a_failed_trial_shows_why(experiment, fake, cli, terminal):
    terminal()
    fake.set(mode="crash")
    code, out, _ = cli("run", experiment())
    assert code == 1
    assert any(
        line.startswith("alpha-1  [░") and "failed: crashed: exit code 3" in line
        for line in last_frame(out)
    )


def test_the_display_never_grows_taller_than_the_terminal(experiment, fake, cli, terminal):
    terminal(lines=8)
    fake.set(sleep=0.2)
    settings = experiment(
        concurrency=2,
        variants=[
            {"name": "v1", "prompt": "prompts/build.md", "model": "m", "repeats": 3},
            {"name": "v2", "prompt": "prompts/build.md", "model": "m", "repeats": 3},
        ],
    )

    code, out, _ = cli("run", settings)

    assert code == 0, out
    final = last_frame(out)
    assert len(final) <= 7
    # Finished Trials fold into one line to make room.
    assert final[2].split() == ["finished:", "v1-1", "v1-2", "v1-3", "v2-1", "v2-2", "v2-3"]


def test_trials_run_again_on_resume_wait_instead_of_showing_their_old_failure(
    experiment, fake, cli, terminal
):
    settings = two_alphas(experiment)
    fake.set(mode="crash")
    assert cli("run", settings)[0] == 1

    terminal()
    fake.set(sleep=0.5)
    code, out, _ = cli("run", settings, "--resume")

    assert code == 0, out
    assert "waiting: alpha-2" in out
    assert "failed: crashed" not in out


def test_the_agents_time_limit_counts_from_when_the_agent_starts(experiment, fake, cli, terminal):
    terminal()
    fake.set(sleep=0.5)
    code, out, _ = cli("run", experiment(pre_build=["sleep 1.2"], limits={"timeout_minutes": 5}))
    assert code == 0, out
    # During pre_build, only how long the Trial has taken; no limit applies yet.
    assert " ?  pre_build: sleep 1.2" in out and "   time 1s" in out
    assert "agent time [░░░░░░░░░░] 0s/5m00s" in out
