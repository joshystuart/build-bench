"""`insights`: a model writes the words of insights.html; the page draws its charts
from the report's own numbers."""

import json
import re
from types import SimpleNamespace

import litellm
import pytest
import replies
from test_report import no_models, saved, trial, write  # noqa: F401


class Writer:
    """Stands in for litellm.completion when the insights writer is an API model.

    `reply` is what it answers; by default, words worked out from the prompt."""

    def __init__(self):
        self.reply = None
        self.requests = []

    def __call__(self, **request):
        self.requests.append(request)
        text = "\n".join(m["content"] for m in request["messages"])
        assert replies.kind(text) == "insights"
        reply = replies.insights_reply(text) if self.reply is None else self.reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(cost=None),
        )


@pytest.fixture
def writer(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test-key")
    found = Writer()
    monkeypatch.setattr(litellm, "completion", found)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.0123)
    return found


def refuse_models(monkeypatch):
    def refuse(**_):
        raise AssertionError("no model should be asked")

    monkeypatch.setattr(litellm, "completion", refuse)


def test_the_writer_is_asked_once_and_the_page_is_drawn(saved, cli, writer):  # noqa: F811
    settings, folder = saved
    write(folder / "trials" / "v1-1" / "prompt.md", "Build the plan in .scratch/plan.\n")
    code, out, _ = cli("insights", settings)
    assert code == 0, out
    [request] = writer.requests
    # The merge Judge writes them when the settings name no other model.
    assert request["model"] == "openai/gpt-5"
    system, user = (m["content"] for m in request["messages"])
    assert '"headline"' in system
    # The setups, ratios worked out so the model need not, the report and the subagents.
    assert "### v1\n\nclaude agent; model m1; 3 Trial(s).\n" in user
    assert "Its prompt:\n\n```\nBuild the plan in .scratch/plan.\n```" in user
    assert "- v2 costs 50% more than v1 ($3.00 against $2.00)." in user
    assert "# The results\n\n# demo\n" in user and "## Differences" in user
    assert "## Subagents\n\n- explore\n" in user

    record = json.loads((folder / "insights.json").read_text())
    assert record["status"] == "ok" and record["model"] == "openai/gpt-5"
    assert record["cost_usd"] == 0.0123
    assert record["words"]["pick"] == "v1"
    assert record["words"]["defect_titles"] == {"D1": "Plain D1", "D2": "Plain D2"}
    assert f"Wrote {folder / 'insights.html'}" in out
    # The report links to the page.
    assert "<a href='insights.html'>" in (folder / "report.html").read_text()
    assert "[insights.html](insights.html)" in (folder / "report.md").read_text()

    # Nothing changed, so nothing is asked; --fresh asks again.
    code, out, _ = cli("insights", settings)
    assert code == 0 and len(writer.requests) == 1
    assert "have not changed" in out
    assert cli("insights", settings, "--fresh")[0] == 0
    assert len(writer.requests) == 2


def test_the_page_draws_the_reports_numbers_around_the_words(saved, cli, writer):  # noqa: F811
    settings, folder = saved
    # Noon UTC, so 1 January wherever the test runs.
    write(folder / "experiment.json", {"name": "demo", "started_at": 1767268800})
    writer.reply = replies.fenced(
        {
            "headline": "<script>alert(1)</script> v1 is *cheaper*",
            "pick": "v1",
            "pick_title": "Use v1",
            # A heading left null gets the page's own; a number is text.
            "sections": {"cost": "v2 costs *more*", "quality": None},
            "roles": [{"name": "Explore", "subagents": ["explore"]}],
            "defect_titles": {"D1": "mul is not there", "D9": "no such Defect"},
            "stats": [{"value": 50, "label": "cost of v2"}] * 5,
        }
    )
    assert cli("insights", settings)[0] == 0
    page = (folder / "insights.html").read_text()

    # The words are text; only their *emphasis* becomes markup.
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; v1 is <em>cheaper</em>" in page
    assert "v2 costs <em>more</em>" in page
    assert page.count("<b>50</b><span>cost of v2") == 3  # the page has room for three stats
    assert "<h2>Defects per build</h2>" in page
    assert "Build experiment · demo · 1 Jan 2026" in page
    assert "Pick: Use v1" in page
    # Works offline, in either theme.
    assert not re.search(r"""(src|href)=['"]?(https?:)?//""", page)
    assert "prefers-color-scheme: dark" in page

    # Cost per finished build: v1 $1 and $3, v2 $2 and $4. The pick is marked.
    cost = page.split("aria-label='Cost per build'")[1].split("</section>")[0]
    rows = re.findall(
        r"<div class='row( pick)?'.*?<span class='name'>(\w+)</span>(.*?)</div>", cost, re.S
    )
    assert [(pick, name) for pick, name, _ in rows] == [(" pick", "v1"), ("", "v2")]
    assert [body.count("class='dot'") for *_, body in rows] == [2, 2]
    assert re.findall(r"<span class='val'>([^<]+)</span>", cost) == ["$2.00", "$3.00"]
    # Defects per built build: v1-1 has D1, v1-2 has D1 and D2; v2-2 has neither.
    quality = page.split("aria-label='Defects per build'")[1].split("</section>")[0]
    assert re.findall(r"<span class='val'>([^<]+)</span>", quality) == ["1.5", "0"]
    # Both Defects differ between builds, named in the writer's words or their own.
    differing = page.split("Where builds differ")[1].split("</section>")[0]
    assert "mul is not there" in differing and "add loses data" in differing
    assert "no such Defect" not in page
    assert differing.count("class='mark present'") == 3

    # The subagent is coloured by its role; v1-2's 350 s idle is drawn as 20 s, so
    # v2-1's 15 minutes set the scale.
    # v1's main agent waited on its subagent for half a minute a build, on average.
    time = page.split("aria-label='Active minutes per build'")[1].split("</section>")[0]
    assert time.count("class='fill wait'") == 1
    assert time.count("class='dot'") == 4  # one per finished build
    assert "<span class='sw r1'></span>Explore" in page
    assert "class='span r1'" in page
    assert "one 15-minute scale of active time" in page
    assert "idle 5m 50s" in page


def test_report_draws_the_page_again_only_from_the_same_results(
    saved,  # noqa: F811
    cli,
    writer,
    monkeypatch,
):
    settings, folder = saved
    assert cli("insights", settings)[0] == 0
    refuse_models(monkeypatch)
    (folder / "insights.html").unlink()
    code, out, _ = cli("report", settings)
    assert code == 0 and f"Wrote {folder / 'insights.html'}" in out

    # Once the results change, the words may no longer fit them.
    trial(folder, "v2-2", cost=9.0, seconds=300)
    code, out, _ = cli("report", settings)
    assert code == 0
    assert "insights.html" not in "".join(line for line in out.splitlines() if "Wrote" in line)
    assert "the results changed since its words were written. `build-bench insights` asks" in out
    assert "href='insights.html'" not in (folder / "report.html").read_text()


def test_a_reply_the_page_cannot_use_fails(saved, cli, writer):  # noqa: F811
    settings, folder = saved
    writer.reply = replies.fenced({"headline": "Fine", "pick": "v9"})
    code, out, _ = cli("insights", settings)
    assert code == 1
    record = json.loads((folder / "insights.json").read_text())
    assert record["status"] == "failed"
    assert record["reason"] == "pick is 'v9', which is no Variant"
    assert "error: the insights failed: pick is 'v9', which is no Variant" in out
    assert (
        "insights.html was not written: the last `build-bench insights` failed "
        "(pick is 'v9', which is no Variant)" in out
    )
    assert not (folder / "insights.html").exists()
    assert (folder / "insights.reply.md").read_text() == writer.reply
    # The report is still written.
    assert (folder / "report.html").is_file()

    # A reply with no JSON in it is asked for once more before it fails.
    writer.reply = "I think v1 is better."
    assert cli("insights", settings)[0] == 1
    assert len(writer.requests) == 3


def test_the_settings_can_name_another_writer(saved, cli, writer, monkeypatch):  # noqa: F811
    settings, folder = saved
    two = [
        {"name": "j", "type": "api", "model": "openai/gpt-5"},
        {"name": "k", "type": "api", "model": "openai/gpt-5-mini"},
    ]
    text = settings.read_text()
    settings.write_text(text + "merge_judge: k\n" + _judges(two))
    assert cli("insights", settings)[0] == 0
    assert writer.requests[-1]["model"] == "openai/gpt-5-mini"

    writer_settings = "insights:\n  model: anthropic/claude-opus-5-5\n  api_key_env: MY_KEY\n"
    settings.write_text(text + writer_settings)
    monkeypatch.delenv("MY_KEY", raising=False)
    code, _, err = cli("insights", settings)
    assert code == 2 and "MY_KEY" in err
    # Only `insights` and `all` ask the writer, so `judge` does not need its key: it
    # gets past the logins to the folder, which is not a results folder.
    code, _, err = cli("judge", settings, "--results", settings.parent)
    assert code == 2 and "not a results folder" in err and "MY_KEY" not in err
    monkeypatch.setenv("MY_KEY", "sk-mine")
    # Another model writes again, though the results have not changed.
    assert cli("insights", settings)[0] == 0
    assert writer.requests[-1]["model"] == "anthropic/claude-opus-5-5"
    assert writer.requests[-1]["api_key"] == "sk-mine"


def _judges(judges: list[dict]) -> str:
    return "judges:\n" + "".join(
        f"  - {{name: {j['name']}, type: {j['type']}, model: {j['model']}}}\n" for j in judges
    )


def test_an_agent_can_write_the_insights(experiment, fake, cli):
    settings = experiment(insights={"type": "claude", "model": "claude-opus-5-5", "effort": "low"})
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo"})
    trial(folder, "alpha-1")
    trial(folder, "alpha-2", cost=2.0)
    code, out, _ = cli("insights", settings)
    assert code == 0, out
    [call] = fake.calls(judge=True, kind="insights")
    assert call["argv"][call["argv"].index("--model") + 1] == "claude-opus-5-5"
    assert "# The results" in call["prompt"]
    record = json.loads((folder / "insights.json").read_text())
    assert record["status"] == "ok" and record["type"] == "claude"
    assert record["cost_usd"] == 0.0123
    assert "Same quality. <em>v1</em> costs less." in (folder / "insights.html").read_text()


def test_no_model_to_write_with(experiment, cli, no_models):  # noqa: F811
    settings = experiment()
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo"})
    code, _, err = cli("insights", settings)
    assert code == 2
    assert "no model to write the insights: add `insights` or a Judge" in err


@pytest.mark.parametrize(
    ("writer_settings", "problem"),
    [
        ({"type": "api", "model": "openai/gpt-5", "effort": "high"}, "insights.effort: only for"),
        (
            {"type": "copilot", "model": "gpt-6.1-terra", "api_key_env": "K"},
            "insights.api_key_env: only for type api",
        ),
    ],
)
def test_settings_the_writer_cannot_use_are_refused(experiment, cli, writer_settings, problem):
    code, _, err = cli("report", experiment(insights=writer_settings))
    assert code == 2 and problem in err
