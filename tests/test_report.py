"""`report` against a saved results folder, plus the HTML page."""

import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

import litellm
import pytest

SCORE_NAMES = ("correctness", "spec_completeness", "code_quality", "test_quality", "honesty")


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def trial(
    folder: Path,
    name: str,
    *,
    status="finished",
    reason=None,
    version="2.1.319",
    active=1,
    cost=1.0,
    tokens=1000,
    seconds=600.0,
    wait=0.0,
    subagents=(),
    lines=(10, 2, 5, 0),
    checks=True,
    ticked=2,
    verdicts=None,
    summary=None,
    diff="+changed\n",
):
    variant, repeat = name.rsplit("-", 1)
    t = folder / "trials" / name
    write(
        t / "status.json",
        {
            "trial": name,
            "variant": variant,
            "repeat": int(repeat),
            "status": status,
            "reason": reason,
            "claude_version": version,
        },
    )
    write(t / "timings.json", {"max_active": active})
    if summary is not None:
        write(t / "summary.md", summary)
    if status != "finished":
        return
    write(
        t / "measurements.json",
        {
            "cost_usd": cost,
            "tokens": {"total": tokens},
            "total_seconds": seconds,
            "wait_seconds": wait,
            "wait_intervals": [[s, e] for _, s, e in subagents],
            "subagents": [{"name": n, "start": s, "end": e} for n, s, e in subagents],
            "lines": dict(
                zip(
                    ("prod_added", "prod_removed", "test_added", "test_removed"), lines, strict=True
                )
            ),
            "checks": {"ran": True, "passed": checks},
            "criteria_ticked": ticked,
        },
    )
    if diff is not None:
        write(t / "diff.patch", diff)
    for judge, v in (verdicts or {}).items():
        write(t / "verdicts" / f"{judge}.json", {"judge": judge, **v})


def ok(score: int, cost=0.1, seconds=30.0) -> dict:
    return {
        "status": "ok",
        "scores": dict.fromkeys(SCORE_NAMES, score),
        "bugs": ["b"],
        "note": "n",
        "cost_usd": cost,
        "seconds": seconds,
    }


@pytest.fixture
def saved(experiment):
    """A results folder with hand-chosen numbers, one failed Trial and one failed Verdict."""
    settings = experiment(
        auth="subscription",
        variants=[
            {"name": "v1", "prompt": "prompts/build.md", "model": "m1", "repeats": 3},
            {"name": "v2", "prompt": "prompts/build.md", "model": "m2", "repeats": 2},
        ],
        judges=[{"name": "j", "type": "api", "model": "openai/gpt-5"}],
    )
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo", "auth": "subscription"})
    trial(
        folder,
        "v1-1",
        cost=1.0,
        tokens=1000,
        seconds=600,
        wait=60,
        subagents=[("explore", 100.0, 160.0)],
        lines=(10, 2, 5, 0),
        checks=True,
        ticked=2,
        verdicts={"j": ok(6, cost=0.1, seconds=30)},
    )
    trial(
        folder,
        "v1-2",
        cost=3.0,
        tokens=3000,
        seconds=1200,
        lines=(20, 4, 15, 0),
        checks=False,
        ticked=4,
        active=2,
        version="2.1.320",
        verdicts={"j": ok(8, cost=0.3, seconds=90)},
    )
    trial(folder, "v1-3", status="failed", reason="crashed: exit code 3")
    trial(
        folder,
        "v2-1",
        cost=2.0,
        tokens=2000,
        seconds=900,
        verdicts={
            "j": {
                "status": "failed",
                "reason": "missing score honesty",
                "cost_usd": 0.2,
                "seconds": 60,
            }
        },
    )
    trial(folder, "v2-2", cost=4.0, tokens=4000, seconds=300, verdicts={"j": ok(7)})
    return settings, folder


@pytest.fixture
def no_models(monkeypatch, tmp_path):
    # Only git (to read the settings' base commit) is on the PATH; no `claude`.
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))

    def refuse(**_):
        raise AssertionError("report must not call a model")

    monkeypatch.setattr(litellm, "completion", refuse)


def test_report_reads_only_the_results_folder(saved, cli, no_models):
    settings, folder = saved
    code, out, _ = cli("report", settings)
    assert code == 0
    for name in ("report.json", "report.md", "report.html"):
        assert (folder / name).is_file()
        assert f"Wrote {folder / name}" in out


def test_report_uses_the_folder_given(saved, cli, no_models):
    settings, folder = saved
    other = folder.parent / "20250101-000000"
    write(other / "experiment.json", {"name": "demo"})
    assert cli("report", settings, "--results", other)[0] == 0
    assert (other / "report.md").is_file() and not (folder / "report.md").exists()


def test_averages_ranges_and_counts(saved, cli, no_models):
    settings, folder = saved
    cli("report", settings)
    data = json.loads((folder / "report.json").read_text())
    v1, v2 = data["variants"]
    assert (v1["name"], v1["trials"], v1["finished"]) == ("v1", 3, 2)
    assert v1["build"]["cost_usd"] == {"mean": 2.0, "min": 1.0, "max": 3.0, "n": 2}
    assert v1["build"]["total_minutes"] == {"mean": 15.0, "min": 10.0, "max": 20.0, "n": 2}
    assert v1["build"]["wait_minutes"] == {"mean": 0.5, "min": 0.0, "max": 1.0, "n": 2}
    assert v1["output"]["prod_added"] == {"mean": 15.0, "min": 10, "max": 20, "n": 2}
    assert v1["output"]["checks_passed"] == {"mean": 0.5, "min": 0.0, "max": 1.0, "n": 2}
    assert v1["output"]["criteria_ticked"] == {"mean": 3.0, "min": 2, "max": 4, "n": 2}
    assert v1["scores"]["j"]["correctness"] == {"mean": 7.0, "min": 6, "max": 8, "n": 2}
    # The failed Verdict on v2-1 is left out of the scores, not the build numbers.
    assert v2["scores"]["j"]["honesty"] == {"mean": 7.0, "min": 7, "max": 7, "n": 1}
    assert v2["build"]["cost_usd"]["n"] == 2

    md = (folder / "report.md").read_text()
    assert "| v1 | 2/3 | 2 (1–3) | 2000 (1000–3000) | 15 (10–20) | 0.5 (0–1) |" in md
    assert "| v2 | 2/2 | 3 (2–4) |" in md


def test_failures_are_listed_and_left_out(saved, cli, no_models):
    settings, folder = saved
    cli("report", settings)
    md = (folder / "report.md").read_text()
    assert "## Failed Trials\n\n- v1-3: crashed: exit code 3" in md
    assert "## Failed Verdicts\n\n- v2-1 by j: missing score honesty" in md
    data = json.loads((folder / "report.json").read_text())
    assert data["failed_trials"] == [
        {"trial": "v1-3", "reason": "crashed: exit code 3", "final_message": None}
    ]
    assert data["empty_trials"] == []
    assert "## Trials that changed nothing" not in md
    assert "What went wrong" in (folder / "report.html").read_text()


def test_the_report_says_why_a_trial_went_wrong(experiment, cli, no_models):
    """A failed Trial and a Trial that built nothing both show the agent's own last words."""
    settings = experiment()
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo"})
    stopped = (
        "I stopped the build before it started. The repository has no git remote.\n\n"
        "**To continue:** add a remote & run it again."
    )
    trial(folder, "alpha-1", diff="", summary=stopped)
    trial(
        folder,
        "alpha-2",
        status="failed",
        reason="budget: reached the spending limit of $5",
        summary="Ran out of budget while writing tests.",
    )
    trial(folder, "alpha-3")
    assert cli("report", settings)[0] == 0

    md = (folder / "report.md").read_text()
    assert (
        "- alpha-2: budget: reached the spending limit of $5\n"
        "  - The agent's last words: “Ran out of budget while writing tests.”"
    ) in md
    assert (
        "## Trials that changed nothing\n\n"
        "These Trials finished but left no code changes. Their numbers are in the averages.\n\n"
        "- alpha-1: “I stopped the build before it started. The repository has no git remote.”"
    ) in md
    assert "alpha-3" not in md.split("## Trials that changed nothing")[1]

    page = (folder / "report.html").read_text()
    problems = page.split("<h2>What went wrong</h2>")[1].split("</ul>")[0]
    assert problems.count("<li>") == 2
    assert "Failed: budget: reached the spending limit of $5" in problems
    assert "Finished but left no code changes" in problems
    # The whole message is one click away, escaped.
    assert "add a remote &amp; run it again." in problems
    assert "changed nothing</text>" in page  # flagged in the timeline too


def test_judge_cost_and_time_are_apart_from_the_build(saved, cli, no_models):
    settings, folder = saved
    cli("report", settings)
    data = json.loads((folder / "report.json").read_text())
    assert data["judges"] == [
        {
            "name": "j",
            "type": "api",
            "model": "openai/gpt-5",
            "verdicts": 3,
            "failed": 1,
            "cost_usd": 0.7,
            "minutes": 3.5,
        }
    ]
    md = (folder / "report.md").read_text()
    assert "| j | api | openai/gpt-5 | 3 | 1 | 0.7 | 3.5 |" in md
    # Build cost is the Trials' alone.
    assert data["variants"][0]["build"]["cost_usd"]["mean"] == 2.0


def test_flags(saved, cli, no_models):
    settings, folder = saved
    cli("report", settings)
    md = (folder / "report.md").read_text()
    assert "Login: **subscription**" in md
    assert "**Ran alongside other Trials:** v1-2 (2 at once)" in md
    assert "**Mixed Claude Code versions:** 2.1.319, 2.1.320" in md


def test_one_version_and_api_key_are_not_flagged(experiment, cli, no_models):
    settings = experiment()
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo", "auth": "api-key"})
    trial(folder, "alpha-1")
    assert cli("report", settings)[0] == 0
    md = (folder / "report.md").read_text()
    assert "Login: **API key**." in md
    assert "Claude Code version: 2.1.319." in md
    assert "Ran alongside" not in md and "Mixed" not in md
    # Measurements but no Verdicts and no Judges still report.
    assert "| alpha | 1/1 | 1 | 1000 | 10 |" in md
    assert "## Scores" not in md


def md_tables(md: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] | None = None
    for line in md.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            if current is None:
                current = []
                tables.append(current)
            current.append([c.strip() for c in line.strip("|").split("|")])
        elif not line.startswith("|---"):
            current = None
    return tables


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self.row = []
            self.tables[-1].append(self.row)
        elif tag in ("td", "th"):
            self.cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.row.append(self.cell.strip())
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data


def test_json_holds_every_number_in_the_markdown(saved, cli, no_models):
    settings, folder = saved
    cli("report", settings)
    data = json.loads((folder / "report.json").read_text())

    found: set[float] = set()

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            found.add(float(value))

    walk(data)
    numbers = 0
    for table in md_tables((folder / "report.md").read_text()):
        for row in table[1:]:
            for cell in row[1:]:
                for n in re.findall(r"\d+(?:\.\d+)?", cell):
                    numbers += 1
                    assert float(n) in found, (n, row)
    assert numbers > 50


def test_html_shows_the_same_numbers_as_the_markdown(saved, cli, no_models):
    settings, folder = saved
    cli("report", settings)
    md = md_tables((folder / "report.md").read_text())
    parser = Tables()
    parser.feed((folder / "report.html").read_text())
    assert len(md) == 4  # build, output, scores, judges
    assert parser.tables[: len(md)] == md


def test_html_is_self_contained_with_shared_scale_timelines(saved, cli, no_models):
    settings, folder = saved
    cli("report", settings)
    page = (folder / "report.html").read_text()
    assert not re.search(r"""(src|href)=['"]?(https?:)?//""", page)
    assert "<link" not in page and "@import" not in page
    # One chart for every finished Trial, grouped by Trial, on one time scale.
    svgs = re.findall(r"<svg .*?</svg>", page, flags=re.S)
    assert len(svgs) == 1
    assert page.count("<summary>Show as table</summary>") == 1
    groups = dict(
        re.findall(r"aria-label='Timeline of ([\w-]+)'>(.*?)</g></g>", svgs[0], flags=re.S)
    )
    assert sorted(groups) == ["v1-1", "v1-2", "v2-1", "v2-2"]

    def main_end(group: str) -> float:
        bars = re.findall(r"class='mark main' x='([\d.]+)' y='[\d.]+' width='([\d.]+)'", group)
        return max(float(x) + float(w) for x, w in bars)

    zero = float(re.search(r"<line class='axis' x1='([\d.]+)'", svgs[0]).group(1))
    # v1-2 ran for 20 minutes, the longest, and fills the scale; v1-1 ran 10, v2-2 five.
    full = main_end(groups["v1-2"]) - zero
    assert main_end(groups["v1-1"]) - zero == pytest.approx(full / 2, abs=0.2)
    assert main_end(groups["v2-2"]) - zero == pytest.approx(full / 4, abs=0.2)
    # v1-1's main agent waited from 100 s to 160 s: two working bars around a wait line.
    v1_1 = groups["v1-1"]
    assert v1_1.count("class='mark main'") == 2 and v1_1.count("class='wait-track'") == 1
    assert v1_1.count("class='mark sub'") == 1 and ">explore<" in v1_1
    assert "class='mark sub'" not in groups["v2-2"] and "wait-track" not in groups["v2-2"]
    assert "10m 00s · 1 subagent<" in v1_1
    assert page.count("tabindex='0'") == 4 + 1  # four main agents, one subagent
    assert "data-tip='1m 00s\nexplore · v1-1\n" in page  # tooltip lines, longest first
    assert "prefers-color-scheme: dark" in page and "max-width: 600px" in page
