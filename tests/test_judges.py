"""API Judges (model call stubbed) and Agent Judges (fake `claude`): the Verdicts
they give, the Defects merged from them and the Defect checks that follow."""

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
import replies
from conftest import press_ctrl_c, results_folder, workspace_home

GOOD = replies.FOUND
NO_SEVERITY = replies.fenced({"findings": [{"title": "mul is missing"}], "note": ""})
BAD_SEVERITY = replies.fenced({"findings": [{"title": "t", "severity": "huge"}], "note": ""})

VARIANTS = [
    {
        "name": "secretvariant",
        "prompt": "prompts/build.md",
        "model": "claude-opus-5-5",
        "repeats": 2,
    },
]


class Stub:
    """Stands in for litellm.completion and records every request.

    `reply` answers requests for a Verdict: a text, or a function of the request's
    number among them. Merge and check requests get a reply worked out from the
    request, unless `merge_reply` is set. `present` is the answer to every Defect check.
    """

    def __init__(self, reply=GOOD, sleep=0.0):
        self.reply = reply
        self.merge_reply = None
        self.present = "yes"
        #: The answer to every Comparison: "1", "2" or "same".
        self.better = "1"
        #: The cost the provider itself reports with each reply, as OpenRouter does.
        self.reported_cost = None
        self.sleep = sleep
        self.requests = []
        self.active = 0
        self.most_active = 0
        self.lock = threading.Lock()

    def __call__(self, **request):
        text = "\n".join(m["content"] for m in request["messages"])
        kind = replies.kind(text)
        with self.lock:
            self.requests.append(request)
            number = len(self.sent("find"))
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        try:
            time.sleep(self.sleep)
            if kind == "merge":
                reply = self.merge_reply or replies.merge_reply(text)
            elif kind == "check":
                reply = replies.check_reply(text, self.present)
            elif kind == "compare":
                reply = replies.compare_reply(self.better)
            else:
                reply = self.reply(number) if callable(self.reply) else self.reply
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                usage=SimpleNamespace(cost=self.reported_cost),
            )
        finally:
            with self.lock:
                self.active -= 1

    def sent(self, kind: str) -> list[dict]:
        """The requests of one step: find, merge, check or compare."""
        return [
            r
            for r in self.requests
            if replies.kind("\n".join(m["content"] for m in r["messages"])) == kind
        ]


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


def saved(settings, *path):
    return json.loads((results_folder(settings).joinpath(*path)).read_text())


def verdict(settings, trial, judge):
    return saved(settings, "trials", trial, "verdicts", f"{judge}.json")


def defect_check(settings, trial, judge):
    return saved(settings, "trials", trial, "defect-checks", f"{judge}.json")


# --- API Judge -------------------------------------------------------------------------


def test_api_judge_gets_the_evidence_and_saves_a_verdict(experiment, fake, cli, stub):
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert len(stub.sent("find")) == 2
    request = stub.sent("find")[0]
    assert request["model"] == "openai/gpt-5"
    text = request["messages"][1]["content"]
    assert "- [ ] add mul" in text  # the plan
    assert "+committed by the fake agent" in text  # the diff against the base
    assert "Command: `echo checks ran ok`, exit code 0." in text and "checks ran ok" in text
    assert "its commits are folded into these changes" in text
    # What is already measured is handed over, not counted again.
    assert "Production lines: 2 added, 0 removed. Test lines: 0 added, 0 removed." in text
    assert "The checks (`echo checks ran ok`) passed." in text
    assert "Your job is to find what is wrong" in request["messages"][0]["content"]

    found = verdict(settings, "secretvariant-1", "diff-only")
    assert found["status"] == "ok"
    assert found["findings"] == [
        {
            "title": "mul is missing",
            "severity": "major",
            "where": "calc.py:2",
            "detail": "The plan asks for mul.",
            "reproduced": True,
        },
        {"title": "odd name", "severity": "nit", "where": "", "detail": "", "reproduced": False},
    ]
    assert found["note"] == "Fine."
    assert found["cost_usd"] == 0.0042
    assert found["seconds"] >= 0
    assert re.fullmatch(r"trial-[0-9a-f]{6}", found["label"])


def test_api_judge_cost_is_what_the_provider_reported_when_the_model_has_no_price(
    experiment, fake, cli, stub, monkeypatch
):
    def no_price(completion_response):
        raise ValueError("This model isn't mapped yet.")

    monkeypatch.setattr(litellm, "completion_cost", no_price)
    stub.reported_cost = 0.0125
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    assert verdict(settings, "secretvariant-1", "diff-only")["cost_usd"] == 0.0125

    # Neither a price nor a reported cost: unknown, not zero.
    stub.reported_cost = None
    settings = built(experiment, cli, judges=[api_judge("another")])
    assert cli("judge", settings)[0] == 0
    assert verdict(settings, "secretvariant-1", "another")["cost_usd"] is None


def test_a_judge_never_sees_what_the_build_said_or_did_to_the_plan(experiment, fake, cli, stub):
    """Both can name the Variant, and every build is held to the plan as first written."""
    fake.set(summary="Autopilot built it with four subagents.")
    settings = built(experiment, cli, judges=[api_judge()])
    cli("judge", settings)
    for request in stub.requests:
        sent = json.dumps(request)
        assert "Autopilot" not in sent
        assert "- [x] add mul" not in sent  # the box the build ticked


def test_api_judge_is_blind(experiment, fake, cli, stub):
    settings = built(experiment, cli, judges=[api_judge()])
    cli("judge", settings)
    labels = set()
    for request in stub.sent("find") + stub.sent("check"):
        sent = json.dumps(request)
        for hidden in ("secretvariant", "claude-opus-5-5", "fake commit by"):
            assert hidden not in sent
        # Each request carries exactly one Trial's label.
        assert len(set(re.findall(r"trial-[0-9a-f]{6}", sent))) == 1
        labels.update(re.findall(r"# Trial (trial-[0-9a-f]{6})", sent))
    # A Trial gets a new label every time it is shown.
    assert len(labels) == 4
    for hidden in ("secretvariant", "claude-opus-5-5", "trial-"):
        assert hidden not in json.dumps(stub.sent("merge"))


@pytest.mark.parametrize(
    "reply, reason",
    [
        (NO_SEVERITY, "finding 1 has severity None, not one of critical, major, minor, nit"),
        (BAD_SEVERITY, "finding 1 has severity 'huge'"),
        ("I liked it.", "the reply holds no JSON object with findings"),
    ],
)
def test_a_bad_reply_is_a_failed_verdict(experiment, fake, cli, stub, reply, reason):
    stub.reply = reply
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 1
    found = verdict(settings, "secretvariant-1", "diff-only")
    assert found["status"] == "failed"
    assert reason in found["reason"]
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
    found = verdict(settings, "secretvariant-1", "diff-only")
    assert found["status"] == "failed"
    assert found["reason"] == "timeout: no reply after 0.005 minutes"
    assert stub.requests[0]["timeout"] == pytest.approx(0.3)


def test_judges_share_concurrency_and_resume_with_a_new_judge(experiment, fake, cli, stub):
    stub.sleep = 0.3
    stub.reply = lambda number: BAD_SEVERITY if number == 1 else GOOD
    variants = [dict(VARIANTS[0], repeats=3)]
    settings = experiment(variants=variants, concurrency=2, judges=[api_judge("one")])
    assert cli("run", settings)[0] == 0
    assert cli("judge", settings)[0] == 1
    assert len(stub.sent("find")) == 3
    assert stub.most_active == 2

    stub.requests.clear()
    stub.reply = GOOD
    settings = experiment(
        variants=variants, concurrency=2, judges=[api_judge("one"), api_judge("two")]
    )
    assert cli("judge", settings)[0] == 0
    # One failed Verdict from judge one, and three new ones from judge two.
    assert len(stub.sent("find")) == 4
    for trial in ("secretvariant-1", "secretvariant-2", "secretvariant-3"):
        for judge in ("one", "two"):
            assert verdict(settings, trial, judge)["status"] == "ok"

    stub.requests.clear()
    assert cli("judge", settings)[0] == 0
    assert stub.requests == []


def test_a_changed_prompt_judges_again(experiment, fake, cli, stub):
    """Verdicts from two different prompts are never mixed in one Experiment."""
    exp = experiment().parent
    (exp / "judges").mkdir()
    (exp / "judges" / "mine.md").write_text('First rules. Reply with "findings".\n')
    settings = built(experiment, cli, judges=[api_judge(prompt="judges/mine.md")])
    assert cli("judge", settings)[0] == 0
    assert len(stub.sent("find")) == 2
    (exp / "judges" / "mine.md").write_text('Stricter rules. Reply with "findings".\n')
    assert cli("judge", settings)[0] == 0
    assert len(stub.sent("find")) == 4
    assert stub.sent("find")[-1]["messages"][0]["content"].startswith("Stricter rules.")


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


def test_a_copilot_judge_logs_in_before_anything_starts(experiment, fake, cli, stub, monkeypatch):
    """LiteLLM logs in to GitHub Copilot with a code typed at github.com, so it
    happens up front, and no variable is asked for."""
    from litellm.llms.github_copilot import authenticator

    logins = []
    broken = [False]

    class Authenticator:
        def get_access_token(self):
            logins.append(len(fake.calls(judge=None)))
            if broken[0]:
                raise RuntimeError("Timed out waiting for user to authorize the device")
            return "gho-token"

    monkeypatch.setattr(authenticator, "Authenticator", Authenticator)
    monkeypatch.setattr(
        litellm, "validate_environment", lambda **_: pytest.fail("this would start the login")
    )
    monkeypatch.delenv("OPENAI_API_KEY")
    settings = experiment(variants=VARIANTS, judges=[api_judge(model="github_copilot/gpt-5")])
    assert cli("all", settings)[0] == 0
    assert logins[0] == 0  # before the first Trial was built
    assert stub.requests[0]["model"] == "github_copilot/gpt-5"

    broken[0] = True
    stub.requests.clear()
    code, _, err = cli("judge", settings)
    assert code == 2
    assert "the GitHub Copilot login failed: Timed out waiting" in err
    assert stub.requests == []


# --- Defects: merged from the Findings, then checked on every Trial ---------------------


def two_findings(first: str, second: str) -> str:
    def one(title):
        return {"title": title, "severity": "minor", "where": "calc.py:1", "detail": f"{title}!"}

    return replies.fenced({"findings": [one(first), one(second)], "note": ""})


def test_findings_are_merged_into_the_experiments_defects(experiment, fake, cli, stub):
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert "[merged] 2 finding(s) into 1 defect(s) by diff-only" in out
    [request] = stub.sent("merge")
    asked = request["messages"][1]["content"]
    assert "None yet." in asked
    assert "### F1: mul is missing\n\nWhere: calc.py:2\n\nThe plan asks for mul." in asked
    assert "### F2: mul is missing" in asked
    assert "odd name" not in asked  # nits are counted, never merged

    merged = saved(settings, "defects.json")
    assert merged["status"] == "ok" and merged["judge"] == "diff-only"
    [defect] = merged["defects"]
    assert (defect["id"], defect["title"], defect["severity"]) == ("D1", "mul is missing", "major")
    assert defect["description"] == "Look for mul in calc.py."
    assert [key.rsplit("/", 1)[0] for key in defect["findings"]] == [
        "secretvariant-1/diff-only",
        "secretvariant-2/diff-only",
    ]


def test_a_defect_is_as_severe_as_its_worst_finding_and_keeps_its_id(experiment, fake, cli, stub):
    settings = built(experiment, cli, judges=[api_judge("one")])
    assert cli("judge", settings)[0] == 0
    stub.requests.clear()

    worse = {"title": "mul loses data", "severity": "critical", "where": "", "detail": ""}
    stub.reply = replies.fenced({"findings": [worse], "note": ""})
    settings = experiment(
        variants=VARIANTS,
        checks="echo checks ran ok",
        judges=[api_judge("one"), api_judge("two")],
    )
    assert cli("judge", settings)[0] == 0
    # Only judge two's new Findings are placed, and the existing Defect is offered for them.
    [request] = stub.sent("merge")
    asked = request["messages"][1]["content"]
    assert "### D1: mul is missing\n\nLook for mul in calc.py." in asked
    assert asked.count("### F") == 2 and "mul loses data" in asked
    [defect] = saved(settings, "defects.json")["defects"]
    assert (defect["id"], defect["severity"], len(defect["findings"])) == ("D1", "critical", 4)


def test_a_finding_the_merge_leaves_out_becomes_its_own_defect(experiment, fake, cli, stub):
    variants = [dict(VARIANTS[0], repeats=1)]
    stub.reply = two_findings("slow add", "wrong sign")
    stub.merge_reply = replies.fenced(
        {"defects": [{"id": "new", "title": "add is slow", "description": "d", "findings": ["F1"]}]}
    )
    settings = experiment(variants=variants, judges=[api_judge()])
    assert cli("run", settings)[0] == 0
    assert cli("judge", settings)[0] == 0
    first, second = saved(settings, "defects.json")["defects"]
    assert (first["id"], first["title"]) == ("D1", "add is slow")
    assert (second["id"], second["title"], second["description"]) == (
        "D2",
        "wrong sign",
        "wrong sign!",
    )


@pytest.mark.parametrize(
    "reply, reason",
    [
        (
            {"defects": [{"id": "new", "title": "t", "findings": ["F9"]}]},
            "defect 1 names unknown finding(s) F9",
        ),
        ({"defects": [{"id": "new", "findings": ["F1"]}]}, "defect 1 is new but has no title"),
        ({"defects": "none"}, "defects is not a list"),
    ],
)
def test_a_bad_merge_fails_and_is_tried_again(experiment, fake, cli, stub, reply, reason):
    stub.merge_reply = replies.fenced(reply)
    settings = built(experiment, cli, judges=[api_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert f"[merge failed] by diff-only: {reason}" in out
    merged = saved(settings, "defects.json")
    assert merged["status"] == "failed" and merged["defects"] == []
    # With no Defects there is nothing to check.
    assert stub.sent("check") == []

    stub.requests.clear()
    stub.merge_reply = None
    assert cli("judge", settings)[0] == 0
    assert len(stub.sent("find")) == 0 and len(stub.sent("merge")) == 1
    assert len(stub.sent("check")) == 2


def test_the_settings_can_name_the_judge_that_merges(experiment, fake, cli, stub):
    judges = [api_judge("one"), api_judge("two", model="openai/gpt-merge")]
    settings = built(experiment, cli, judges=judges, merge_judge="two")
    assert cli("judge", settings)[0] == 0
    [request] = stub.sent("merge")
    assert request["model"] == "openai/gpt-merge"
    assert saved(settings, "defects.json")["judge"] == "two"

    settings = experiment(variants=VARIANTS, judges=judges, merge_judge="three")
    code, _, err = cli("judge", settings)
    assert code == 2
    assert "merge_judge: no judge named 'three'" in err


def test_every_judge_checks_every_trial_for_every_defect(experiment, fake, cli, stub):
    stub.present = "no"
    settings = built(experiment, cli, judges=[api_judge("one"), api_judge("two")])
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert len(stub.sent("check")) == 4
    assert "[checked] secretvariant-2 by two" in out
    request = stub.sent("check")[0]
    assert "decide whether this build has it" in request["messages"][0]["content"]
    asked = request["messages"][1]["content"]
    assert "+committed by the fake agent" in asked  # the same evidence as for a Verdict
    assert asked.endswith(
        "## Known defects\n\n### D1: mul is missing\n\nLook for mul in calc.py.\n"
    )

    checked = defect_check(settings, "secretvariant-2", "two")
    assert checked["status"] == "ok"
    assert checked["answers"] == {
        "D1": {"present": "no", "severity": None, "where": "calc.py:2", "evidence": "e"}
    }
    assert checked["cost_usd"] == 0.0042


def test_only_new_defects_are_checked_later(experiment, fake, cli, stub):
    variants = [dict(VARIANTS[0], repeats=1)]
    settings = experiment(variants=variants, judges=[api_judge("one")])
    assert cli("run", settings)[0] == 0
    assert cli("judge", settings)[0] == 0
    stub.requests.clear()

    # A second Judge finds something else, which becomes a second Defect.
    stub.reply = two_findings("slow add", "wrong sign")
    stub.merge_reply = replies.fenced(
        {"defects": [{"id": "new", "title": "slow", "description": "d", "findings": ["F1", "F2"]}]}
    )
    settings = experiment(variants=variants, judges=[api_judge("one"), api_judge("two")])
    assert cli("judge", settings)[0] == 0
    one, two = stub.sent("check")
    asked = {r["messages"][1]["content"].split("## Known defects")[1] for r in (one, two)}
    # Judge one had answered for D1 already; judge two answers for both.
    assert sorted(a.count("### D") for a in asked) == [1, 2]
    checked = defect_check(settings, "secretvariant-1", "one")
    assert sorted(checked["answers"]) == ["D1", "D2"]
    assert checked["cost_usd"] == pytest.approx(0.0084)


def test_a_check_that_skips_a_defect_fails(experiment, fake, cli, stub, monkeypatch):
    settings = built(experiment, cli, judges=[api_judge()])
    monkeypatch.setattr(replies, "check_reply", lambda *_: replies.fenced({"answers": []}))
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert "[check failed] secretvariant-1 by diff-only: no answer for D1" in out
    assert defect_check(settings, "secretvariant-1", "diff-only")["status"] == "failed"


# --- Agent Judge -------------------------------------------------------------------------


def test_a_judge_that_says_yes_gives_the_severity_there(experiment, fake, cli, stub, monkeypatch):
    settings = built(experiment, cli, judges=[api_judge()])
    assert cli("judge", settings)[0] == 0
    request = stub.sent("check")[0]
    assert "give one severity" in request["messages"][0]["content"]
    answer = defect_check(settings, "secretvariant-1", "diff-only")["answers"]["D1"]
    assert (answer["present"], answer["severity"]) == ("yes", "minor")

    # A yes with no severity, or one that is not a Defect's, fails the check.
    def no_severity(prompt, present="yes"):
        return replies.fenced({"answers": [{"id": "D1", "present": "yes", "severity": "nit"}]})

    monkeypatch.setattr(replies, "check_reply", no_severity)
    settings = built(experiment, cli, judges=[api_judge("another")])
    code, out, _ = cli("judge", settings)
    assert code == 1
    assert "D1 is present with severity 'nit', not one of critical, major, minor" in out


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
    calls = fake.calls(judge=True, kind="find")
    assert len(calls) == 2
    call = calls[0]
    assert "judge-was-here.txt" in call["cwd_files_after"]
    assert not str(call["cwd"]).startswith(str(trial.parent))
    assert call["config_files"] == []
    assert call["env"]["ANTHROPIC_API_KEY"]
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--effort") + 1] == "medium"
    found = verdict(settings, "secretvariant-1", "blind-agent")
    assert found["status"] == "ok"
    assert [f["title"] for f in found["findings"]] == ["mul is missing", "odd name"]
    assert found["cost_usd"] == 0.0123
    # Its output is kept beside the Verdict.
    assert (trial / "verdicts" / "blind-agent.stream.jsonl").is_file()


def test_agent_judge_is_blind(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    cli("judge", settings)
    for call in fake.calls(judge=True, kind="find") + fake.calls(judge=True, kind="check"):
        seen = json.dumps(
            [call["prompt"], call["cwd"], call["cwd_files"], call["git_log"], call["git_reflog"]]
        )
        for hidden in ("secretvariant", "claude-opus-5-5", "fake commit by"):
            assert hidden not in seen
        assert re.search(r"# Trial trial-[0-9a-f]{6}", call["prompt"])
        # The build's changes are there to inspect, uncommitted on the base.
        assert "fake_committed.txt" in call["cwd_files"]
        # The plan is the one first written: the box the build ticked is unticked again.
        assert "- [ ] add mul" in call["plan_text"] and "[x]" not in call["plan_text"]
        assert "Fake summary" not in call["prompt"]


def test_the_default_judge_prompt(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    cli("judge", settings)
    prompt = fake.calls(judge=True, kind="find")[0]["prompt"]
    for expected in [
        "Your job is to find what is wrong with this build.",
        "1. **Plan requirements that are missing or only partly built.**",
        "2. **Wrong behaviour.**",
        "3. **Tests that do not prove what they claim,**",
        "4. **Code that will cost the next developer:**",
        "try to reproduce it with a failing\ntest",
        "- **critical**: loses or damages data",
        "- **nit**: nothing behaves wrongly.",
        "The plan is in `.scratch/plan/`, as it was before the build started.",
        "The project's checks command is `echo checks ran ok`.",
        "its commits are folded into these changes",
        "The checks (`echo checks ran ok`) passed.",
    ]:
        assert expected in prompt
    for gone in ("Score each", "ticked"):
        assert gone not in prompt


def test_a_judge_can_use_its_own_prompt(experiment, fake, cli):
    exp = experiment().parent
    (exp / "judges").mkdir()
    (exp / "judges" / "mine.md").write_text('My own rules. Reply with "findings" and more.\n')
    settings = built(experiment, cli, judges=[agent_judge(prompt="judges/mine.md")])
    cli("judge", settings)
    assert fake.calls(judge=True, kind="find")[0]["prompt"].startswith("My own rules.")


def test_an_agent_judge_merges_in_an_empty_folder_and_checks_in_a_copy(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    [merge] = fake.calls(judge=True, kind="merge")
    assert merge["cwd_files"] == [] and merge["config_files"] == []
    assert "### F1: mul is missing" in merge["prompt"]
    assert saved(settings, "defects.json")["cost_usd"] == 0.0123

    checks = fake.calls(judge=True, kind="check")
    assert len(checks) == 2
    assert "fake_committed.txt" in checks[0]["cwd_files"]
    assert checks[0]["prompt"].endswith(
        "## Known defects\n\n### D1: mul is missing\n\nLook for mul in calc.py.\n"
    )
    checked = defect_check(settings, "secretvariant-1", "blind-agent")
    assert checked["answers"]["D1"]["present"] == "yes"
    trial = results_folder(settings) / "trials" / "secretvariant-1"
    assert (trial / "defect-checks" / "blind-agent.stream.jsonl").is_file()


@pytest.mark.parametrize(
    "control, reason",
    [
        ({"judge_reply": NO_SEVERITY}, "finding 1 has severity None"),
        ({"judge_reply": BAD_SEVERITY}, "finding 1 has severity 'huge'"),
        ({"judge_mode": "crash"}, "crashed: exit code 3"),
    ],
)
def test_agent_judge_failures(experiment, fake, cli, control, reason):
    settings = built(experiment, cli, judges=[agent_judge()])
    fake.set(**control)
    assert cli("judge", settings)[0] == 1
    found = verdict(settings, "secretvariant-1", "blind-agent")
    assert found["status"] == "failed"
    assert reason in found["reason"]


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
    found = verdict(settings, "secretvariant-1", "blind-agent")
    assert found["reason"] == "timeout: stopped after 0.01 minutes"
    argv = fake.calls(judge=True)[0]["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "2.0"

    fake.set()
    assert cli("judge", settings)[0] == 0
    # Two that hung, then two Verdicts, one merge and two Defect checks.
    assert len(fake.calls(judge=True)) == 7
    assert cli("judge", settings)[0] == 0
    assert len(fake.calls(judge=True)) == 7


def test_ctrl_c_stops_an_api_judge_quietly(experiment, fake, cli, stub):
    settings = built(experiment, cli, judges=[api_judge()])
    stub.sleep = 600
    press_ctrl_c(lambda: stub.requests)
    started = time.time()

    code, out, err = cli("judge", settings)

    assert time.time() - started < 15
    assert code == 130
    assert "Traceback" not in out + err and err.strip() == "Stopped."
    found = verdict(settings, "secretvariant-1", "diff-only")
    assert found["reason"] == "stopped: build-bench was stopped (Ctrl+C)"


def test_ctrl_c_stops_an_agent_judge_quietly(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    fake.set(judge_mode="hang")
    press_ctrl_c(lambda: len(fake.calls(judge=True)) == 1)
    started = time.time()

    code, out, err = cli("judge", settings)

    assert time.time() - started < 15
    assert code == 130
    assert "Traceback" not in out + err and "Stopped." in err
    found = verdict(settings, "secretvariant-1", "blind-agent")
    assert found["reason"] == "stopped: build-bench was stopped (Ctrl+C)"


def test_agent_judge_rebuilds_a_removed_workspace(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    assert cli("clean", settings)[0] == 0
    code, out, _ = cli("judge", settings)
    assert code == 0, out
    assert verdict(settings, "secretvariant-1", "blind-agent")["status"] == "ok"
    for call in fake.calls(judge=True, kind="find"):
        # The build's changes, committed or not, are back from the saved diff.
        assert {"fake_committed.txt", "fake_uncommitted.txt"} <= set(call["cwd_files"])
        assert "- [ ] add mul" in call["plan_text"]
        assert "files git ignores" in call["prompt"]
        assert "fake commit by" not in json.dumps([call["git_log"], call["git_reflog"]])


def test_agent_judge_reports_a_workspace_it_cannot_rebuild(experiment, fake, cli):
    settings = built(experiment, cli, judges=[agent_judge()])
    assert cli("clean", settings)[0] == 0
    patch = results_folder(settings) / "trials" / "secretvariant-1" / "diff.patch"
    patch.write_text("not a diff\n")
    assert cli("judge", settings)[0] == 1
    found = verdict(settings, "secretvariant-1", "blind-agent")
    assert found["status"] == "failed"
    assert "could not be rebuilt from its saved diff" in found["reason"]
