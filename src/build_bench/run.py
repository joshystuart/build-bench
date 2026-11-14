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
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import nullcontext
from pathlib import Path

import yaml

from .agent import COPILOT_LOGIN, STOPPED, AgentRequest, Agents, Headless
from .progress import Live
from .results import (
    Trial,
    new_workspaces,
    planned_trials,
    read_json,
    saved_trials,
    workspaces,
    write_json,
)
from .settings import Settings

WARNING = """\
WARNING: build-bench starts coding agents with every permission check bypassed.
They run commands on this machine, as you, in your real home folder, without
asking. Each Trial works in its own clone with no git remote, but nothing else
is sandboxed. Only run Experiments whose prompts, plans and repositories you trust."""

#: How to stop a run, shown while Trials run.
HOW_TO_STOP = "Ctrl+C stops the run; `build-bench run --resume` finishes it later."


class RunError(Exception):
    pass


def in_parallel(work, items: list, concurrency: int, agent: Agents, say) -> None:
    """Call `work` on each item, `concurrency` at a time.

    On Ctrl+C no more items start, the running agents are stopped, and each
    `work` call still returns and saves why it ended; then KeyboardInterrupt
    is raised again. Another Ctrl+C while waiting kills the agents at once.
    """
    pool = ThreadPoolExecutor(max_workers=concurrency)
    futures = [pool.submit(work, item) for item in items]
    try:
        _wait(futures)
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        agent.stop()
        told = False
        while True:
            try:
                if not told:
                    told = True
                    say("Stopping: waiting for the agents to end (Ctrl+C again to kill them now)…")
                _wait(futures)
                break
            except KeyboardInterrupt:
                agent.stop(now=True)
        pool.shutdown()
        raise
    pool.shutdown()
    for future in futures:
        future.result()


def _wait(futures: list) -> None:
    # In short waits, not one long one: a Ctrl+C cannot interrupt a thread
    # blocked on a lock everywhere, but it is seen between two waits.
    while wait(futures, timeout=0.2).not_done:
        pass


def preflight(settings: Settings, agents: Agents) -> dict[str, str]:
    """Check what must hold before the first agent starts; return each agent's version."""
    versions = {}
    for kind in settings.agents(judges=False):
        agent = agents.of(kind)
        try:
            versions[kind] = agent.version()
        except (FileNotFoundError, RuntimeError) as error:
            raise RunError(str(error)) from None
        pin = settings.version_pin(kind)
        if pin and versions[kind] != pin:
            raise RunError(f"{agent.name} {versions[kind]} is installed but the settings pin {pin}")
    return versions


def secrets(settings: Settings) -> list[str]:
    """Every login value an agent may be handed, so none is left in a saved file."""
    names = [settings.auth_variable] if settings.auth else []
    return [os.environ[n] for n in [*names, *COPILOT_LOGIN] if os.environ.get(n)]


def inherited_instructions(folder: Path) -> list[Path]:
    """CLAUDE.md files above the workspaces, which Claude Code loads like the repo's own."""
    found = []
    for parent in [folder, *folder.parents]:
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            if (parent / name).is_file():
                found.append(parent / name)
    return found


def save_skills(settings: Settings, folder: Path) -> None:
    """Copy each Variant's skills into the results folder the first time it runs there.

    Trials take their skills from this copy, so a resumed Experiment runs the
    same skills as its first Trials even if the originals have changed since.
    """
    for variant in settings.variants:
        saved = folder / "skills" / variant.name
        if not variant.skills or saved.exists():
            continue
        for skill in variant.skills:
            # Links are followed, so the copy holds what they pointed at.
            shutil.copytree(skill, saved / skill.name)


def start_folder(settings: Settings, folder: Path, versions: dict[str, str]) -> None:
    """Save the Experiment's own files the first time a results folder is used."""
    save_skills(settings, folder)
    if (folder / "experiment.json").exists():
        experiment = read_json(folder / "experiment.json")
        for kind, version in versions.items():
            seen = experiment.setdefault(f"{kind}_versions", [])
            if version not in seen:
                seen.append(version)
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
            "workspaces": str(new_workspaces(folder)),
            "auth": settings.auth,
            **{f"{kind}_version": version for kind, version in versions.items()},
            **{f"{kind}_versions": [version] for kind, version in versions.items()},
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
    agent: Agents | None = None,
    say=print,
    live: bool = False,
) -> list[Trial]:
    """Build every missing or failed Trial. Returns the Trials that failed.

    With `live`, a progress display is redrawn below the printed lines while
    the Trials run; it needs a terminal.
    """
    agent = agent or Agents()
    versions = preflight(settings, agent)
    say(WARNING)
    start_folder(settings, folder, versions)
    inherited = inherited_instructions(workspaces(folder) or folder)
    if inherited:
        say(
            "WARNING: Claude Code also reads instruction files in folders above a Trial's\n"
            "workspace, so these reach every Trial. Move them:\n"
            + "\n".join(f"  {p}" for p in inherited)
        )

    planned = planned_trials(settings, folder)
    todo = [t for t in planned if not t.finished]
    skipped = len(planned) - len(todo)
    if skipped:
        say(f"Skipping {skipped} finished Trial(s).")

    lock = threading.Lock()
    display = Live(settings, planned, todo, hint=HOW_TO_STOP) if live and todo else None
    # With the display on, lines print above it instead of through it.
    tell = display.say if display else say
    if todo and not display:
        say(HOW_TO_STOP)

    def one(trial: Trial) -> None:
        with lock:
            tell(f"[start] {trial.id}")
        status = build_trial(settings, folder, trial, agent, versions)
        with lock:
            if status["status"] == "finished":
                tell(f"[done]  {trial.id} finished in {_minutes(status['agent_seconds'])}")
            else:
                tell(f"[fail]  {trial.id} {status['reason']}")

    with display or nullcontext():
        in_parallel(one, todo, settings.concurrency, agent, tell)

    record_overlap(folder)
    return [t for t in planned_trials(settings, folder) if not t.finished]


def _minutes(seconds: float | None) -> str:
    seconds = seconds or 0
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def build_trial(
    settings: Settings, folder: Path, trial: Trial, agents: Agents, versions: dict[str, str]
) -> dict:
    variant = next(v for v in settings.variants if v.name == trial.variant)
    agent = agents.of(variant.agent)
    if trial.folder.exists():
        # A failed Trial is run again from nothing, never patched up.
        shutil.rmtree(trial.folder)
    if trial.home.exists():
        shutil.rmtree(trial.home)
    trial.folder.mkdir(parents=True)
    status = {
        "trial": trial.id,
        "variant": trial.variant,
        "repeat": trial.repeat,
        "status": "running",
        "reason": None,
        "agent": variant.agent,
        "agent_version": versions[variant.agent],
        # Kept under its first name too, which older reports read.
        "claude_version": versions.get("claude") if variant.agent == "claude" else None,
        "auth": settings.auth if variant.agent == "claude" else None,
        "agent_seconds": None,
    }
    write_json(trial.folder / "status.json", status)
    try:
        status.update(_build(settings, folder, trial, agent))
    except Exception as error:  # noqa: BLE001 - any surprise fails this Trial only
        status.update(status="failed", reason=f"crashed: {type(error).__name__}: {error}")
    if status["status"] != "finished" and agent.stopped:
        # Whatever broke (a pre_build step or git also got the Ctrl+C), the
        # stop is why; the Trial runs again on resume.
        status.update(status="failed", reason=STOPPED)
    _redact(trial.folder, secrets(settings))
    write_json(trial.folder / "status.json", status)
    return status


def _build(settings: Settings, folder: Path, trial: Trial, agent: Headless) -> dict:
    variant = next(v for v in settings.variants if v.name == trial.variant)
    repo = trial.workspace
    config = trial.home / "config"
    make_workspace(settings, folder, repo)
    if variant.config is not None:
        shutil.copytree(variant.config, config, symlinks=True)
    else:
        config.mkdir(parents=True)
    if (folder / "skills" / variant.name).is_dir():
        skills = config / "skills"
        if variant.config is not None and skills.is_symlink():
            # Copy what the link points at, so the Variant's skills never land in it.
            # Resolved from the original config, where a relative link still works.
            target = (variant.config / "skills").resolve()
            skills.unlink()
            shutil.copytree(target, skills, symlinks=True)
        shutil.copytree(folder / "skills" / variant.name, skills, dirs_exist_ok=True)

    pre_build_seconds = 0.0
    with (trial.folder / "pre_build.log").open("w") as log:
        for step in settings.pre_build:
            if agent.stopped:
                return {"status": "failed", "reason": STOPPED}
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
        auth=settings.agent_login(variant.agent),
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
    if result.summary is not None:
        (trial.folder / "summary.md").write_text(result.summary)
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
    # Fetching a commit that is not a branch tip needs protocol v2 (git >= 2.26).
    git(
        repo,
        "-c",
        "protocol.version=2",
        "fetch",
        "--quiet",
        "--no-tags",
        str(settings.repo),
        f"{base}:refs/heads/build-bench",
    )
    git(repo, "checkout", "--quiet", "build-bench")
    if settings.plan_rel:
        shutil.copytree(folder / "plan", repo / settings.plan_rel, dirs_exist_ok=True)


def save_transcript(agent: Headless, request: AgentRequest, into: Path) -> None:
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
        # Both sides side by side in one folder, so the diff names short relative paths.
        with tempfile.TemporaryDirectory(prefix="build-bench-plan-") as tmp:
            after = "plan-after"
            shutil.copytree(folder / "plan", Path(tmp) / "plan", symlinks=True)
            if (repo / settings.plan_rel).is_dir():
                shutil.copytree(repo / settings.plan_rel, Path(tmp) / after, symlinks=True)
            plan_diff = subprocess.run(
                ["git", "diff", "--no-index", "--no-prefix", "plan", after],
                cwd=tmp,
                capture_output=True,
                text=True,
            ).stdout
        # Name both sides by the plan's path in the repository.
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


def _redact(trial_folder: Path, hidden: list[str]) -> None:
    """Belt and braces: no saved file may hold a login's value."""
    hidden = [secret for secret in hidden if len(secret) >= 8]
    if not hidden:
        return
    top = [p for p in trial_folder.iterdir() if p.name != "workspace"]
    files = [f for p in top for f in ([p] if p.is_file() else p.rglob("*")) if f.is_file()]
    for path in files:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if any(secret in text for secret in hidden):
            for secret in hidden:
                text = text.replace(secret, "[redacted]")
            path.write_text(text)


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
