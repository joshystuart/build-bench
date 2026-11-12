"""`all` and `clean`, end to end with a fake `claude` and a stubbed model call."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
from conftest import results_folder, workspace_home

REPLY = (
    '```json\n{"scores": {"correctness": 7, "spec_completeness": 6, "code_quality": 8, '
    '"test_quality": 5, "honesty": 9}, "bugs": [], "note": "ok"}\n```'
)
JUDGES = [
    {"name": "api", "type": "api", "model": "openai/gpt-5"},
    {"name": "agent", "type": "claude", "model": "claude-sonnet-5-5"},
]
VARIANTS = [
    {"name": "v1", "prompt": "prompts/build.md", "model": "m1", "repeats": 2},
    {"name": "v2", "prompt": "prompts/build.md", "model": "m2"},
]


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test-key")
    calls = []

    def completion(**request):
        calls.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=REPLY))])

    monkeypatch.setattr(litellm, "completion", completion)
    return calls


def test_all_runs_judges_and_reports_into_one_folder(experiment, fake, cli, model):
    settings = experiment(variants=VARIANTS, judges=JUDGES, checks="true")
    code, out, _ = cli("all", settings)
    assert code == 0, out
    [folder] = (settings.parent / ".bench" / "results" / "demo").iterdir()
    for trial in ("v1-1", "v1-2", "v2-1"):
        t = folder / "trials" / trial
        assert json.loads((t / "status.json").read_text())["status"] == "finished"
        assert (t / "measurements.json").is_file()
        for judge in ("api", "agent"):
            assert json.loads((t / "verdicts" / f"{judge}.json").read_text())["status"] == "ok"
    for name in ("report.json", "report.md", "report.html"):
        assert (folder / name).is_file()
    assert len(model) == 3
    assert len(fake.calls(judge=True)) == 3


def test_all_carries_on_past_a_failed_trial_and_exits_non_zero(experiment, fake, cli, model):
    fake.set(per_cwd={"/v1-2/": "crash"})
    settings = experiment(variants=VARIANTS, judges=JUDGES)
    code, out, _ = cli("all", settings)
    assert code == 1
    folder = results_folder(settings)
    assert len(model) == 2
    assert not (folder / "trials/v1-2/verdicts").exists()
    md = (folder / "report.md").read_text()
    assert "## Failed Trials\n\n- v1-2: crashed: exit code 3" in md
    assert "| v1 | 1/2 |" in md


def test_all_checks_every_key_before_building(experiment, fake, cli, model, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    code, _, err = cli("all", experiment(variants=VARIANTS, judges=JUDGES))
    assert code == 2
    assert "OPENAI_API_KEY" in err
    assert fake.calls(judge=None) == []


def files(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def test_clean_removes_workspaces_and_keeps_results(experiment, fake, cli, model):
    settings = experiment(variants=VARIANTS, judges=JUDGES[:1])
    assert cli("all", settings)[0] == 0
    folder = results_folder(settings)
    before = files(folder)
    homes = [workspace_home(folder / "trials" / t) for t in ("v1-1", "v1-2", "v2-1")]
    assert all((home / "repo").is_dir() for home in homes)
    code, out, _ = cli("clean", settings)
    assert code == 0
    assert f"Removed 3 workspace(s) from {folder}" in out
    assert not homes[0].parent.exists()
    assert files(folder) == before
    for kept in ("transcript/main.jsonl", "diff.patch", "measurements.json", "verdicts/api.json"):
        assert f"trials/v1-1/{kept}" in before
    assert "report.md" in before

    # Reports and API Judges still work from the saved files.
    (folder / "report.md").unlink()
    assert cli("report", settings)[0] == 0
    assert (folder / "report.md").is_file()
    later = experiment(
        variants=VARIANTS,
        judges=[*JUDGES[:1], {"name": "later", "type": "api", "model": "openai/gpt-5"}],
    )
    model.clear()
    assert cli("judge", later)[0] == 0
    assert len(model) == 3
    assert "+committed by the fake agent" in model[0]["messages"][1]["content"]


def test_the_installed_command(tmp_path):
    """The real console script, as `uv tool install` puts it on the PATH."""
    script = Path(sys.executable).parent / "build-bench"
    done = subprocess.run([script, "--help"], capture_output=True, text=True, env=os.environ)
    assert done.returncode == 0
    for command in ("run", "judge", "report", "all", "clean"):
        assert command in done.stdout
