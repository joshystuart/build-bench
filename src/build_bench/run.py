"""`run`: build every Trial in its own clone and save what happened."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from .agent import Agent, AgentRequest, ClaudeCode
from .results import (
    Trial,
    planned_trials,
    read_json,
    saved_trials,
    write_json,
)
from .settings import DEFAULT_AUTH_ENV, Settings

WARNING = """\
WARNING: build-bench starts coding agents with every permission check bypassed.
They run commands on this machine, as you, in your real home folder, without
asking. Each Trial works in its own clone with no git remote, but nothing else
is sandboxed. Only run Experiments whose prompts, plans and repositories you trust."""


class RunError(Exception):
    pass


def preflight(settings: Settings, agent: Agent) -> str:
    """Check what must hold before the first Trial; return the agent's version."""
    try:
        version = agent.version()
    except (FileNotFoundError, RuntimeError) as error:
        raise RunError(str(error)) from None
    if settings.claude_version and version != settings.claude_version:
        raise RunError(
            f"Claude Code {version} is installed but the settings pin {settings.claude_version}"
        )
    return version


def inherited_instructions(folder: Path) -> list[Path]:
    """CLAUDE.md files above the workspaces, which Claude Code loads like the repo's own."""
    found = []
    for parent in [folder, *folder.parents]:
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            if (parent / name).is_file():
                found.append(parent / name)
    return found


def start_folder(settings: Settings, folder: Path, version: str) -> None:
    """Save the Experiment's own files the first time a results folder is used."""
    if (folder / "experiment.json").exists():
        experiment = read_json(folder / "experiment.json")
        versions = experiment.setdefault("claude_versions", [])
        if version not in versions:
            versions.append(version)
        write_json(folder / "experiment.json", experiment)
        return
    assert settings.path is not None
    shutil.copyfile(settings.path, folder / "settings.yml")
    if settings.plan is not None:
        shutil.copytree(settings.plan, folder / "plan", symlinks=True)
    write_json(
        folder / "experiment.json",
        {
            "name": settings.name,
            "settings_file": str(settings.path),
            "repo": str(settings.repo),
            "base": settings.base,
            "base_sha": settings.base_sha,
            "plan": settings.plan_rel,
            "auth": settings.auth,
            "claude_version": version,
            "claude_versions": [version],
            "started_at": time.time(),
            # The parsed settings as data; the file never holds secrets, only
            # the names of the variables that do.
            "settings": yaml.safe_load(settings.path.read_text()),
        },
    )


def run(
    settings: Settings,
    folder: Path,
    *,
    agent: Agent | None = None,
    say=print,
) -> list[Trial]:
    """Build every missing or failed Trial. Returns the Trials that failed."""
    agent = agent or ClaudeCode()
    secret = os.environ[settings.auth_variable]
    version = preflight(settings, agent)
    say(WARNING)
    inherited = inherited_instructions(folder)
    if inherited:
        say(
            "WARNING: Claude Code also reads instruction files in folders above a Trial's\n"
            "workspace, so these reach every Trial. Move them, or move the settings file:\n"
            + "\n".join(f"  {p}" for p in inherited)
        )
    start_folder(settings, folder, version)

    todo = [t for t in planned_trials(settings, folder) if not t.finished]
    skipped = len(planned_trials(settings, folder)) - len(todo)
    if skipped:
        say(f"Skipping {skipped} finished Trial(s).")

    lock = threading.Lock()

    def one(trial: Trial) -> None:
        with lock:
            say(f"[start] {trial.id}")
        status = build_trial(settings, folder, trial, agent, version, secret)
        with lock:
            if status["status"] == "finished":
                say(f"[done]  {trial.id} finished in {_minutes(status['agent_seconds'])}")
            else:
                say(f"[fail]  {trial.id} {status['reason']}")

    with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
        list(pool.map(one, todo))

    record_overlap(folder)
    return [t for t in planned_trials(settings, folder) if not t.finished]


def _minutes(seconds: float | None) -> str:
    seconds = seconds or 0
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def build_trial(
    settings: Settings, folder: Path, trial: Trial, agent: Agent, version: str, secret: str
) -> dict:
    if trial.folder.exists():
        # A failed Trial is run again from nothing, never patched up.
        shutil.rmtree(trial.folder)
    trial.folder.mkdir(parents=True)
    status = {
        "trial": trial.id,
        "variant": trial.variant,
        "repeat": trial.repeat,
        "status": "running",
        "reason": None,
        "claude_version": version,
        "auth": settings.auth,
        "agent_seconds": None,
    }
    write_json(trial.folder / "status.json", status)
    try:
        status.update(_build(settings, folder, trial, agent, secret))
    except Exception as error:  # noqa: BLE001 - any surprise fails this Trial only
        status.update(status="failed", reason=f"crashed: {type(error).__name__}: {error}")
    _redact(trial.folder, secret)
    write_json(trial.folder / "status.json", status)
    return status


def _build(settings: Settings, folder: Path, trial: Trial, agent: Agent, secret: str) -> dict:
    variant = next(v for v in settings.variants if v.name == trial.variant)
    repo = trial.workspace
    config = trial.folder / "workspace" / "config"
    make_workspace(settings, folder, repo)
    if variant.config is not None:
        shutil.copytree(variant.config, config, symlinks=True)
    else:
        config.mkdir(parents=True)

    pre_build_seconds = 0.0
    with (trial.folder / "pre_build.log").open("w") as log:
        for step in settings.pre_build:
            log.write(f"$ {step}\n")
            log.flush()
            started = time.time()
            done = subprocess.run(step, shell=True, cwd=repo, stdout=log, stderr=log)
            pre_build_seconds += time.time() - started
            if done.returncode != 0:
                write_json(trial.folder / "timings.json", {"pre_build_seconds": pre_build_seconds})
                return {
                    "status": "failed",
                    "reason": f"pre_build: `{step}` exited with code {done.returncode}",
                }

    prompt = variant.prompt.read_text().replace("{{plan}}", settings.plan_rel or "")
    (trial.folder / "prompt.md").write_text(prompt)
    request = AgentRequest(
        prompt=prompt,
        cwd=repo,
        config_dir=config,
        model=variant.model,
        effort=variant.effort,
        session_id=str(uuid.uuid4()),
        stdout_path=trial.folder / "stream.jsonl",
        stderr_path=trial.folder / "stderr.log",
        auth=(DEFAULT_AUTH_ENV[settings.auth], secret),
        timeout_minutes=settings.limits.timeout_minutes,
        max_budget_usd=settings.limits.max_budget_usd,
    )
    result = agent.start(request)
    write_json(
        trial.folder / "timings.json",
        {
            "pre_build_seconds": pre_build_seconds,
            "agent_seconds": result.seconds,
            "agent_started_at": result.started_at,
            "agent_ended_at": result.ended_at,
            "session_id": request.session_id,
        },
    )
    save_transcript(agent, request, trial.folder / "transcript")
    if result.result and isinstance(result.result.get("result"), str):
        (trial.folder / "summary.md").write_text(result.result["result"])
    save_diffs(settings, folder, trial)
    if result.failure:
        return {"status": "failed", "reason": result.failure, "agent_seconds": result.seconds}
    return {"status": "finished", "agent_seconds": result.seconds}


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def make_workspace(settings: Settings, folder: Path, repo: Path) -> None:
    """A repository holding only the base commit and its history, with no remote,
    and the plan copied in. Nothing past the base (later commits, branches, tags,
    reflogs) is fetched, so the agent cannot read the finished work."""
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet")
    base = settings.base_sha or settings.base
    git(repo, "fetch", "--quiet", "--no-tags", str(settings.repo), f"{base}:refs/heads/build-bench")
    git(repo, "checkout", "--quiet", "build-bench")
    if settings.plan_rel:
        shutil.copytree(folder / "plan", repo / settings.plan_rel, dirs_exist_ok=True)


def save_transcript(agent: Agent, request: AgentRequest, into: Path) -> None:
    main, subagents = agent.transcript(request)
    if main is None:
        return
    into.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(main, into / "main.jsonl")
    if subagents is not None:
        shutil.copytree(subagents, into / "subagents")


def save_diffs(settings: Settings, folder: Path, trial: Trial) -> None:
    """Everything that differs from the base commit, committed or not, as one diff.

    Files git ignores are left out. The plan folder is diffed on its own, against
    the copy saved when the Experiment started.
    """
    repo = trial.workspace
    base = settings.base_sha or settings.base
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        if (repo / ".git" / "index").exists():
            shutil.copyfile(repo / ".git" / "index", index)
        env = {"GIT_INDEX_FILE": str(index)}
        git(repo, "add", "--all", env=env)
        tree = git(repo, "write-tree", env=env).strip()
    # The plan is diffed on its own below, whether git tracks it or not.
    paths = ["--", ".", f":(exclude,literal){settings.plan_rel}"] if settings.plan_rel else []
    (trial.folder / "diff.patch").write_text(git(repo, "diff", base, tree, *paths))
    # Whole files on both sides, so measurements can see code around each change.
    (trial.folder / "diff-full.patch").write_text(git(repo, "diff", "-U100000", base, tree, *paths))

    plan_diff = ""
    if settings.plan_rel:
        after = (repo / settings.plan_rel).relative_to(folder).as_posix()
        plan_diff = subprocess.run(
            ["git", "diff", "--no-index", "--no-prefix", "plan", after],
            cwd=folder,
            capture_output=True,
            text=True,
        ).stdout
        # Name both sides by the plan's path in the repository, never by the
        # Trial folder (which names the Variant).
        header = re.compile(r"^(diff --git |--- |\+\+\+ )(.*)$", re.M)
        plan_diff = header.sub(
            lambda m: (
                m.group(1)
                + re.sub(
                    rf"(^| )(plan|{re.escape(after)})/",
                    lambda p: (
                        f"{p.group(1)}{'a' if p.group(2) == 'plan' else 'b'}/{settings.plan_rel}/"
                    ),
                    m.group(2),
                )
            ),
            plan_diff,
        )
    (trial.folder / "plan.diff").write_text(plan_diff)


def _redact(trial_folder: Path, secret: str) -> None:
    """Belt and braces: no saved file may hold the login's value."""
    if len(secret) < 8:
        return
    top = [p for p in trial_folder.iterdir() if p.name != "workspace"]
    files = [f for p in top for f in ([p] if p.is_file() else p.rglob("*")) if f.is_file()]
    for path in files:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if secret in text:
            path.write_text(text.replace(secret, "[redacted]"))


def record_overlap(folder: Path) -> None:
    """Save, for each Trial, the most Trials whose agents were running at once during it."""
    spans = {}
    for trial in saved_trials(folder):
        timings = read_json(trial.folder / "timings.json") or {}
        if timings.get("agent_started_at") is not None:
            spans[trial] = (timings["agent_started_at"], timings["agent_ended_at"], timings)
    for trial, (start, end, timings) in spans.items():
        points = [start] + [s for s, _, _ in spans.values() if start <= s < end]
        timings["max_active"] = max(
            sum(1 for s, e, _ in spans.values() if s <= p < e) for p in points
        )
        write_json(trial.folder / "timings.json", timings)
