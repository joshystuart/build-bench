"""What `judge` shows while it works: a line as each step starts, and in a
terminal a display of the step's asks."""

import re

from test_judges import BAD_SEVERITY, agent_judge, api_judge, built, stub  # noqa: F401
from test_progress import TESTS, last_frame, terminal  # noqa: F401


def squeezed(out: str) -> str:
    """The output with each run of spaces made one, so columns can be ignored."""
    return re.sub(r" {2,}", " ", out)


def test_each_step_says_what_it_will_do(experiment, fake, cli, stub):  # noqa: F811
    settings = built(experiment, cli, judges=[api_judge()])

    code, out, _ = cli("judge", settings)

    assert code == 0, out
    assert "[judge] measure: 2 Trial(s) to measure, 0 already measured" in out
    assert (
        "[judge] find: 2 Verdict(s) to ask for, 0 already saved "
        "(1 Judge(s) × 2 Trial(s), 1 at a time)"
    ) in out
    assert "[judge] merge: 2 new Finding(s) to place among 0 Defect(s), by diff-only" in out
    assert "[judge] check: 2 of 2 Judge × Trial ask(s) about 1 Defect(s), 1 at a time" in out
    assert "[judge] compare" not in out  # no Judge compares
    assert "[judge] finished in " in out
    # The lines come before what they announce.
    assert out.index("[judge] find:") < out.index("[verdict] ") < out.index("[judge] merge:")

    code, out, _ = cli("judge", settings)

    assert code == 0, out
    assert "[judge] measure: 0 Trial(s) to measure, 2 already measured" in out
    assert "[judge] find: all 2 Verdict(s) already saved" in out
    assert "[judge] merge: no new Findings to merge (1 Defect(s) so far)" in out
    assert "[judge] check: every Trial already checked for all 1 Defect(s)" in out


def test_a_step_that_fails_says_how_to_try_again(experiment, fake, cli, stub):  # noqa: F811
    stub.reply = BAD_SEVERITY
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert "some steps failed (see above), `build-bench judge` tries them again" in out


def test_the_display_shows_each_running_api_judge_and_what_waits(
    experiment,
    fake,
    cli,
    stub,  # noqa: F811
    terminal,  # noqa: F811
):
    settings = built(
        experiment, cli, judges=[api_judge(compare={"max_pairs": 1})], limits={"timeout_minutes": 5}
    )
    terminal()
    stub.sleep = 0.4

    code, out, _ = cli("judge", settings)
    out = squeezed(out)

    assert code == 0, out
    # Which step it is in, and how far each step has got.
    assert "Judging measure ✓ · find 0/2 · merge · check · compare" in out
    assert "find [" in out and "0/2 done · 1 running · 0 failed" in out
    # The running ask, against its Judge's limit; an API Judge says nothing until it replies.
    assert "secretvariant-1 by diff-only api openai/gpt-5 time [" in out and "/5m00s" in out
    assert "now: waiting for the reply" in out
    assert "waiting: secretvariant-2 by diff-only" in out
    assert "time left in this step: unknown until an ask finishes" in out
    assert "time left in this step: ~" in out and "(from 1 finished ask(s))" in out
    assert "Ctrl+C stops judging" in out
    assert "secretvariant-1 by diff-only done in " in out
    assert "merge by diff-only" in out
    # The lines still print, above the display.
    assert "[verdict] secretvariant-1 by diff-only" in out
    # Once judging ends only the steps are left, each done.
    final = last_frame(out)
    assert final[0].startswith("Judging measure ✓ · find ✓ · merge ✓ · check ✓ · compare ✓ · ")
    assert not any("Ctrl+C" in line for line in final)


def test_the_display_follows_a_running_agent_judge(
    experiment,
    fake,
    cli,
    terminal,  # noqa: F811
):
    settings = built(
        experiment,
        cli,
        judges=[agent_judge(limits={"timeout_minutes": 5, "max_budget_usd": 2})],
    )
    terminal()
    fake.set(sleep=0.5, judge_events=[TESTS])

    code, out, _ = cli("judge", settings)
    out = squeezed(out)

    assert code == 0, out
    assert "secretvariant-1 by blind-agent agent claude-sonnet-5-5 time [" in out
    assert "cost [░░░░░░░░░░] $0.00/$2.00" in out
    assert "now: Bash Run the tests · " in out


def test_a_failed_ask_shows_why(experiment, fake, cli, stub, terminal):  # noqa: F811
    stub.reply = BAD_SEVERITY
    settings = built(experiment, cli, judges=[api_judge()])
    terminal()
    stub.sleep = 0.2

    code, out, _ = cli("judge", settings)
    out = squeezed(out)

    assert code == 1
    assert "secretvariant-1 by diff-only failed: finding 1 has severity 'huge'" in out
    assert last_frame(out)[0].startswith("Judging measure ✓ · find ✗ 2 failed · merge ✓ · check ✓")
