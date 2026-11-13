import _thread
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from build_bench.cli import main

HERE = Path(__file__).parent
API_KEY = "sk-ant-api03-test-secret-value-1234"
OAUTH_TOKEN = "sk-ant-oat01-test-secret-token-5678"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture(autouse=True)
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Trial workspaces go under $XDG_CACHE_HOME; keep them in the test's own folder."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A tiny repository with a plan that git ignores, as plans usually are."""
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".scratch/\nbuild/\n")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    plan = repo / ".scratch" / "plan"
    plan.mkdir(parents=True)
    (plan / "PLAN.md").write_text("# Plan\n\n- [ ] add mul\n- [ ] add tests\n")
    # A later commit the Trials must not see.
    (repo / "later.txt").write_text("after the base\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "later")
    return repo


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Put the fake `claude` first on the PATH and give the test its control file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(HERE / 'fake_claude.py')!r}, run_name='__main__')\n"
    )
    script.chmod(0o755)
    control = tmp_path / "fake-control.json"
    log = tmp_path / "fake-log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_CONTROL", str(control))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN)
    return Fake(control, log)


class Fake:
    def __init__(self, control: Path, log: Path):
        self.control = control
        self.log = log
        self.set()

    def set(self, **values) -> None:
        self.control.write_text(json.dumps(values))

    def calls(self, judge: bool | None = False) -> list[dict]:
        found = (
            [json.loads(p.read_text()) for p in self.log.glob("*.json")]
            if self.log.exists()
            else []
        )
        found.sort(key=lambda c: c["start"])
        return [c for c in found if judge is None or c["judge"] == judge]


@pytest.fixture
def experiment(tmp_path: Path, target_repo: Path):
    """Write a settings file beside its prompts; keyword arguments override fields."""

    def make(**overrides) -> Path:
        folder = tmp_path / "exp"
        (folder / "prompts").mkdir(parents=True, exist_ok=True)
        (folder / "prompts" / "build.md").write_text("Build the plan in {{plan}}. Tick each box.\n")
        (folder / "prompts" / "plain.md").write_text("Add a mul function.\n")
        settings = {
            "name": "demo",
            "repo": "../target",
            "base": "HEAD~1",
            "plan": ".scratch/plan",
            "auth": "api-key",
            "concurrency": 1,
            "variants": [
                {
                    "name": "alpha",
                    "prompt": "prompts/build.md",
                    "model": "claude-opus-5-5",
                    "effort": "high",
                },
            ],
        }
        settings.update(overrides)
        settings = {k: v for k, v in settings.items() if v is not None}
        path = folder / "settings.yml"
        path.write_text(yaml.safe_dump(settings, sort_keys=False))
        return path

    return make


@pytest.fixture
def cli(capsys: pytest.CaptureFixture):
    """Run the real command in-process; return (exit code, stdout, stderr)."""

    def call(*args: str) -> tuple[int, str, str]:
        code = main([str(a) for a in args])
        out = capsys.readouterr()
        return code, out.out, out.err

    return call


def results_folder(settings: Path) -> Path:
    folders = sorted((settings.parent / ".bench" / "results").glob("*/*"))
    assert folders, "no results folder"
    return folders[-1]


def workspace_home(trial: Path) -> Path:
    """Where a Trial's clone and config live: outside the results folder."""
    experiment = json.loads((trial.parent.parent / "experiment.json").read_text())
    return Path(experiment["workspaces"]) / trial.name


def press_ctrl_c(ready, times: int = 1, gap: float = 1.0) -> None:
    """Once `ready()` holds, press Ctrl+C `times` times, `gap` seconds apart, as a
    person at the terminal would: the command's main thread gets KeyboardInterrupt."""

    def press() -> None:
        while not ready():
            time.sleep(0.05)
        for _ in range(times):
            _thread.interrupt_main()
            time.sleep(gap)

    threading.Thread(target=press, daemon=True).start()
