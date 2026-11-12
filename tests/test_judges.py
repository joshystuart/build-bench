"""API Judges (model call stubbed) and Agent Judges (fake `claude`)."""

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
from conftest import results_folder, workspace_home

GOOD = """## 1. Summary
Looks fine.

## 8. Verdict

```json
{"scores": {"correctness": 7, "spec_completeness": 6, "code_quality": 8,
  "test_quality": 5, "honesty": 9}, "bugs": ["calc.py:2 no tests"], "note": "Fine."}
```
"""
MISSING = '```json\n{"scores": {"correctness": 7, "code_quality": 8, "test_quality": 5, "honesty": 9}, "bugs": [], "note": ""}\n```'
OUT_OF_RANGE = '```json\n{"scores": {"correctness": 11, "spec_completeness": 6, "code_quality": 8, "test_quality": 0, "honesty": 9}, "bugs": [], "note": ""}\n```'

VARIANTS = [
    {
        "name": "secretvariant",
        "prompt": "prompts/build.md",
        "model": "claude-opus-5-5",
        "repeats": 2,
    },
]


class Stub:
    """Stands in for litellm.completion and records every request."""

    def __init__(self, reply=GOOD, sleep=0.0):
        self.reply = reply
        self.sleep = sleep
        self.requests = []
        self.active = 0
        self.most_active = 0
        self.lock = threading.Lock()

    def __call__(self, **request):
        with self.lock:
            self.requests.append(request)
            number = len(self.requests)
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        try:
            time.sleep(self.sleep)
            reply = self.reply(number) if callable(self.reply) else self.reply
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
            )
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test-key")
    found = Stub()
    monkeypatch.setattr(litellm, "completion", found)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.0042)
    return found


def api_judge(name="diff-only", **extra):
    return {"name": name, "type": "api", "model": "openai/gpt-5", **extra}


def built(experiment, cli, **extra):
    settings = experiment(variants=VARIANTS, checks="echo checks ran ok", **extra)
    assert cli("run", settings)[0] == 0
    return settings


def verdict(settings, trial, judge):
    return json.loads(
        (results_folder(settings) / "trials" / trial / "verdicts" / f"{judge}.json").read_text()
    )


# --- API Judge -------------------------------------------------------------------------


def test_api_judge_gets_the_evidence_and_saves_a_verdict(experiment, fake, cli, stub):
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert len(stub.requests) == 2
    request = stub.requests[0]
    assert request["model"] == "openai/gpt-5"
    text = request["messages"][1]["content"]
    assert "- [ ] add mul" in text  # the plan
    assert "+committed by the fake agent" in text  # the diff against the base
    assert "Command: `echo checks ran ok`, exit code 0." in text and "checks ran ok" in text
    assert "Fake summary: all done." in text
    assert "its commits are folded into these changes" in text
    assert "Score each of these from 1 to 10" in request["messages"][0]["content"]

    saved = verdict(settings, "secretvariant-1", "diff-only")
    assert saved["status"] == "ok"
    assert saved["scores"] == {
        "correctness": 7,
        "spec_completeness": 6,
        "code_quality": 8,
        "test_quality": 5,
        "honesty": 9,
    }
    assert saved["bugs"] == ["calc.py:2 no tests"]
    assert saved["note"] == "Fine."
    assert saved["cost_usd"] == 0.0042
    assert saved["seconds"] >= 0
    assert re.fullmatch(r"trial-[0-9a-f]{6}", saved["label"])


def test_api_judge_is_blind(experiment, fake, cli, stub):
    settings = built(experiment, cli, judges=[api_judge()])
    cli("judge", settings)
    labels = set()
    for request in stub.requests:
        sent = json.dumps(request)
        for hidden in ("secretvariant", "claude-opus-5-5", "fake commit by"):
            assert hidden not in sent
        labels.update(re.findall(r"# Trial (trial-[0-9a-f]{6})", sent))
    assert len(labels) == 2
    # Each request carries exactly one Trial's label.
    for request in stub.requests:
        assert len(set(re.findall(r"trial-[0-9a-f]{6}", json.dumps(request)))) == 1


@pytest.mark.parametrize(
    "reply, reason",
    [
        (MISSING, "missing score spec_completeness"),
        (OUT_OF_RANGE, "score correctness is 11, outside 1 to 10"),
        ("I liked it.", "the reply holds no verdict JSON with scores"),
    ],
)
def test_a_bad_reply_is_a_failed_verdict(experiment, fake, cli, stub, reply, reason):
    stub.reply = reply
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 1
    saved = verdict(settings, "secretvariant-1", "diff-only")
    assert saved["status"] == "failed"
    assert reason in saved["reason"]
    assert "[verdict failed] secretvariant-1 by diff-only" in out


def test_a_judges_own_limits_win_and_a_hang_fails_the_verdict(experiment, fake, cli, stub):
    stub.sleep = 3
    settings = built(
        experiment,
        cli,
        limits={"timeout_minutes": 10},
        judges=[api_judge(limits={"timeout_minutes": 0.005})],
    )
    started = time.time()
    assert cli("judge", settings)[0] == 1
    assert time.time() - started < 6
    saved = verdict(settings, "secretvariant-1", "diff-only")
    assert saved["status"] == "failed"
    assert saved["reason"] == "timeout: no reply after 0.005 minutes"
    assert stub.requests[0]["timeout"] == pytest.approx(0.3)


def test_judges_share_concurrency_and_resume_with_a_new_judge(experiment, fake, cli, stub):
    stub.sleep = 0.3
    stub.reply = lambda number: OUT_OF_RANGE if number == 1 else GOOD
    variants = [dict(VARIANTS[0], repeats=3)]
    settings = experiment(variants=variants, concurrency=2, judges=[api_judge("one")])
    assert cli("run", settings)[0] == 0
    assert cli("judge", settings)[0] == 1
    assert len(stub.requests) == 3
    assert stub.most_active == 2

    stub.requests.clear()
    stub.reply = GOOD
    settings = experiment(
        variants=variants, concurrency=2, judges=[api_judge("one"), api_judge("two")]
    )
    assert cli("judge", settings)[0] == 0
    # One failed Verdict from judge one, and three new ones from judge two.
    assert len(stub.requests) == 4
    folder = results_folder(settings)
    for trial in ("secretvariant-1", "secretvariant-2", "secretvariant-3"):
        for judge in ("one", "two"):
            assert (
                json.loads((folder / "trials" / trial / "verdicts" / f"{judge}.json").read_text())[
                    "status"
                ]
                == "ok"
            )


def test_a_missing_judge_key_stops_judge_before_any_call(experiment, fake, cli, stub, monkeypatch):
    settings = built(experiment, cli, judges=[api_judge(api_key_env="MY_JUDGE_KEY")])
    code, _, err = cli("judge", settings)
    assert code == 2
    assert "missing environment variable(s): MY_JUDGE_KEY" in err
    assert stub.requests == []
    assert not (results_folder(settings) / "trials/secretvariant-1/measurements.json").exists()

    monkeypatch.setenv("MY_JUDGE_KEY", "sk-judge-key-value")
    assert cli("judge", settings)[0] == 0
    assert stub.requests[0]["api_key"] == "sk-judge-key-value"


def test_the_providers_usual_variable_is_required(experiment, fake, cli, stub, monkeypatch):
    settings = built(experiment, cli, judges=[api_judge()])
    monkeypatch.delenv("OPENAI_API_KEY")
    code, _, err = cli("judge", settings)
    assert code == 2
    assert "OPENAI_API_KEY" in err
    assert stub.requests == []


# --- Agent Judge -------------------------------------------------------------------------


def agent_judge(**extra):
    return {"name": "blind-agent", "type": "claude", "model": "claude-sonnet-5-5", **extra}


def tree_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        digest.update(str(path.relative_to(folder)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_agent_judge_works_in_a_throwaway_copy(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge(effort="medium")])
    trial = results_folder(settings) / "trials" / "secretvariant-1"
    before = tree_hash(workspace_home(trial))
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert tree_hash(workspace_home(trial)) == before
    calls = fake.calls(judge=True)
    assert len(calls) == 2
    call = calls[0]
    assert "judge-was-here.txt" in call["cwd_files_after"]
    assert not str(call["cwd"]).startswith(str(trial.parent))
    assert call["config_files"] == []
    assert call["env"]["ANTHROPIC_API_KEY"]
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--effort") + 1] == "medium"
    saved = verdict(settings, "secretvariant-1", "blind-agent")
    assert saved["status"] == "ok"
    assert saved["scores"]["correctness"] == 7
    assert saved["cost_usd"] == 0.0123


def test_agent_judge_is_blind(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    cli("judge", settings)
    for call in fake.calls(judge=True):
        seen = json.dumps(
            [call["prompt"], call["cwd"], call["cwd_files"], call["git_log"], call["git_reflog"]]
        )
        for hidden in ("secretvariant", "claude-opus-5-5", "fake commit by"):
            assert hidden not in seen
        assert re.search(r"# Trial trial-[0-9a-f]{6}", call["prompt"])
        # The build's changes are there to inspect, uncommitted on the base.
        assert "fake_committed.txt" in call["cwd_files"]


def test_the_default_judge_prompt(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    cli("judge", settings)
    prompt = fake.calls(judge=True)[0]["prompt"]
    for expected in [
        "1. **Metrics.**",
        "2. **Claims against reality.**",
        "3. **Run the checks.**",
        "4. **Acceptance criteria and bugs.**",
        '5 means "mergeable after notable fixes"',
        '8 means\n"mergeable with minor nits"',
        "1. Summary\n2. Metrics\n3. Claims against reality\n4. Checks\n"
        "5. Acceptance criteria\n6. Bugs\n7. Scores (each score with a one-line reason)\n"
        "8. Verdict",
        "The project's checks command is `echo checks ran ok`.",
        "its commits are folded into these changes",
    ]:
        assert expected in prompt


def test_a_judge_can_use_its_own_prompt(experiment, fake, cli):
    exp = experiment().parent
    (exp / "judges").mkdir()
    (exp / "judges" / "mine.md").write_text('My own rules. Reply with "correctness" and more.\n')
    settings = built(experiment, cli, judges=[agent_judge(prompt="judges/mine.md")])
    cli("judge", settings)
    assert fake.calls(judge=True)[0]["prompt"].startswith("My own rules.")


@pytest.mark.parametrize(
    "control, reason",
    [
        ({"judge_reply": MISSING}, "missing score spec_completeness"),
        ({"judge_reply": OUT_OF_RANGE}, "outside 1 to 10"),
        ({"judge_mode": "crash"}, "crashed: exit code 3"),
    ],
)
def test_agent_judge_failures(experiment, fake, cli, control, reason):
    settings = built(experiment, cli, judges=[agent_judge()])
    fake.set(**control)
    assert cli("judge", settings)[0] == 1
    saved = verdict(settings, "secretvariant-1", "blind-agent")
    assert saved["status"] == "failed"
    assert reason in saved["reason"]


def test_agent_judge_limits_and_resume(experiment, fake, cli):
    settings = built(
        experiment,
        cli,
        limits={"timeout_minutes": 10},
        judges=[agent_judge(limits={"timeout_minutes": 0.01, "max_budget_usd": 2})],
    )
    fake.set(judge_mode="hang")
    started = time.time()
    assert cli("judge", settings)[0] == 1
    assert time.time() - started < 20
    saved = verdict(settings, "secretvariant-1", "blind-agent")
    assert saved["reason"] == "timeout: stopped after 0.01 minutes"
    argv = fake.calls(judge=True)[0]["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "2.0"

    fake.set()
    assert cli("judge", settings)[0] == 0
    assert len(fake.calls(judge=True)) == 4
    assert cli("judge", settings)[0] == 0
    assert len(fake.calls(judge=True)) == 4


def test_agent_judge_reports_a_removed_workspace(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    assert cli("clean", settings)[0] == 0
    assert cli("judge", settings)[0] == 1
    saved = verdict(settings, "secretvariant-1", "blind-agent")
    assert saved["status"] == "failed"
    assert "workspace is gone" in saved["reason"]
    assert fake.calls(judge=True) == []
