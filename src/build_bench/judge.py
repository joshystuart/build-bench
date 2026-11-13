"""`judge`: measure each finished Trial, then ask each Judge for a Verdict."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from . import lines, transcript
from .agent import STOPPED, AgentRequest, ClaudeCode
from .results import Trial, read_json, saved_trials, write_json
from .run import in_parallel
from .settings import DEFAULT_AUTH_ENV, Judge, Settings, path_rules

SCORES = ("correctness", "spec_completeness", "code_quality", "test_quality", "honesty")
#: Judges are blind, so the build's own commits (whose messages can name the
#: model) are never shown. Say so, or a Judge reads the summary's commit ids as lies.
FOLDED = (
    "Whether the build committed its work is hidden from you on purpose: its commits "
    "are folded into these changes, so commit ids or messages its summary mentions "
    "will not be found. Do not count that against the build."
)
#: Checks output longer than this is cut to its end before a Judge sees it.
CHECKS_LIMIT = 30_000


def judge(settings: Settings, folder: Path, say=print, agent: ClaudeCode | None = None) -> bool:
    """Measure, then judge. Returns False if any Verdict failed."""
    trials = saved_trials(folder)
    for trial in trials:
        measure_trial(settings, folder, trial, say)
    return run_judges(settings, folder, trials, say, agent or ClaudeCode())


# --- Automatic measurements -------------------------------------------------


def measure_trial(settings: Settings, folder: Path, trial: Trial, say=print) -> dict | None:
    target = trial.folder / "measurements.json"
    if not trial.finished:
        say(f"[skip]  {trial.id}: the Trial failed, so it is not measured")
        return None
    if target.exists():
        return read_json(target)

    prices = {**transcript.default_prices(), **settings.prices}
    measured: dict[str, Any] = {}
    try:
        measured.update(transcript.measure(trial.folder / "transcript", prices))
    except (FileNotFoundError, ValueError) as error:
        measured["transcript_error"] = str(error)

    test_paths, ignore_paths, rust_inline = path_rules(settings)
    diff = _text(trial.folder / "diff-full.patch") or _text(trial.folder / "diff.patch")
    measured["lines"] = lines.count_lines(diff, test_paths, ignore_paths, rust_inline)
    measured["criteria_ticked"] = lines.criteria_ticked(_text(trial.folder / "plan.diff"))
    measured["checks"] = run_checks(settings, trial)
    write_json(target, measured)
    say(f"[measured] {trial.id}")
    return measured


def _text(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() else ""


def run_checks(settings: Settings, trial: Trial) -> dict:
    """Run `checks` in a throwaway copy, so the saved workspace never changes."""
    if not settings.checks:
        return {"ran": False, "reason": "the settings name no checks command"}
    if not trial.workspace.is_dir():
        return {"ran": False, "reason": "the Trial's workspace is gone (removed by clean)"}
    timeout = settings.limits.timeout_minutes
    with tempfile.TemporaryDirectory(prefix="build-bench-checks-") as tmp:
        copy = Path(tmp) / "trial"
        shutil.copytree(trial.workspace, copy, symlinks=True)
        started = time.time()
        try:
            done = subprocess.run(
                settings.checks,
                shell=True,
                cwd=copy,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout * 60 if timeout else None,
            )
            output, code = done.stdout, done.returncode
        except subprocess.TimeoutExpired as error:
            output, code = error.stdout or b"", None
    text = output.decode(errors="replace")
    (trial.folder / "checks.log").write_text(text)
    return {
        "ran": True,
        "command": settings.checks,
        "passed": code == 0,
        "exit_code": code,
        "timed_out": code is None,
        "seconds": round(time.time() - started, 3),
    }


# --- Verdicts ----------------------------------------------------------------


class VerdictError(Exception):
    pass


def parse_verdict(reply: str) -> dict:
    """The verdict JSON from a Judge's reply, checked against the fixed shape."""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", reply, flags=re.S)
    decoder = json.JSONDecoder()
    if not candidates:
        for i in [m.start() for m in re.finditer(r"\{", reply)]:
            try:
                value, _ = decoder.raw_decode(reply[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "scores" in value:
                candidates.append(json.dumps(value))
    for text in reversed(candidates):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "scores" in value:
            return check_verdict(value)
    raise VerdictError("the reply holds no verdict JSON with scores")


def check_verdict(value: dict) -> dict:
    scores = value.get("scores")
    if not isinstance(scores, dict):
        raise VerdictError("scores is not an object")
    problems = []
    clean = {}
    for name in SCORES:
        score = scores.get(name)
        if score is None:
            problems.append(f"missing score {name}")
        elif isinstance(score, bool) or not isinstance(score, int | float) or score != int(score):
            problems.append(f"score {name} is not a whole number: {score!r}")
        elif not 1 <= score <= 10:
            problems.append(f"score {name} is {score}, outside 1 to 10")
        else:
            clean[name] = int(score)
    if problems:
        raise VerdictError("; ".join(problems))
    bugs = value.get("bugs", [])
    if not isinstance(bugs, list):
        raise VerdictError("bugs is not a list")
    note = value.get("note", "")
    return {
        "scores": clean,
        "bugs": [b if isinstance(b, str) else json.dumps(b) for b in bugs],
        "note": note if isinstance(note, str) else json.dumps(note),
    }


def judge_prompt(j: Judge) -> str:
    if j.prompt is not None:
        return j.prompt.read_text()
    return (resources.files("build_bench") / "prompts" / "judge.md").read_text()


def run_judges(
    settings: Settings, folder: Path, trials: list[Trial], say, agent: ClaudeCode
) -> bool:
    todo = []
    for j in settings.judges:
        for trial in trials:
            if not (trial.finished and (trial.folder / "measurements.json").exists()):
                continue
            saved = read_json(trial.folder / "verdicts" / f"{j.name}.json")
            if saved and saved.get("status") == "ok":
                continue
            todo.append((j, trial))
    lock = threading.Lock()

    def one(task: tuple[Judge, Trial]) -> dict:
        j, trial = task
        verdict = judge_one(settings, folder, j, trial, agent)
        with lock:
            if verdict["status"] == "ok":
                say(f"[verdict] {trial.id} by {j.name}")
            else:
                say(f"[verdict failed] {trial.id} by {j.name}: {verdict['reason']}")
        return verdict

    in_parallel(one, todo, settings.concurrency, agent, say)

    failed = [
        (j, t)
        for j in settings.judges
        for t in trials
        if t.finished
        and (read_json(t.folder / "verdicts" / f"{j.name}.json") or {}).get("status") != "ok"
    ]
    return not failed


def judge_one(settings: Settings, folder: Path, j: Judge, trial: Trial, agent: ClaudeCode) -> dict:
    label = f"trial-{secrets.token_hex(3)}"
    started = time.time()
    verdict: dict[str, Any] = {
        "judge": j.name,
        "type": j.type,
        "model": j.model,
        "label": label,
        "status": "failed",
        "reason": None,
        "cost_usd": None,
        "seconds": None,
    }
    verdicts = trial.folder / "verdicts"
    verdicts.mkdir(exist_ok=True)
    try:
        if j.type == "api":
            reply, cost = _ask_api(settings, folder, j, trial, label, agent)
        else:
            reply, cost = _ask_agent(settings, folder, j, trial, label, agent)
        verdict["cost_usd"] = cost
        (verdicts / f"{j.name}.reply.md").write_text(reply)
        verdict.update(parse_verdict(reply), status="ok")
    except (VerdictError, JudgeFailed, TimeoutError) as error:
        verdict["reason"] = str(error) or type(error).__name__
    except Exception as error:  # noqa: BLE001 - a broken call fails this Verdict only
        verdict["reason"] = f"{type(error).__name__}: {error}"
    verdict["seconds"] = round(time.time() - started, 3)
    write_json(verdicts / f"{j.name}.json", verdict)
    return verdict


class JudgeFailed(Exception):
    pass


def evidence(settings: Settings, folder: Path, trial: Trial, label: str) -> str:
    """What an API Judge reads: plan, diff, checks output and final summary."""
    parts = [f"# Trial {label}\n"]
    plan = folder / "plan"
    if plan.is_dir():
        parts.append(f"## The plan (at `{settings.plan_rel}` in the repository)\n")
        for f in sorted(p for p in plan.rglob("*") if p.is_file()):
            parts.append(f"### {f.relative_to(plan).as_posix()}\n\n{_text(f)}\n")
        plan_diff = _text(trial.folder / "plan.diff")
        if plan_diff.strip():
            parts.append(f"## Changes the build made to the plan\n\n```diff\n{plan_diff}```\n")
    else:
        parts.append("## The plan\n\nThere is no plan folder; the task was given as a prompt.\n")
    diff = _text(trial.folder / "diff.patch")
    parts.append(f"## Diff against the base commit\n\n{FOLDED}\n\n```diff\n{diff}```\n")
    checks = (read_json(trial.folder / "measurements.json") or {}).get("checks") or {}
    if checks.get("ran"):
        output = _text(trial.folder / "checks.log")
        if len(output) > CHECKS_LIMIT:
            output = "[earlier output cut]\n" + output[-CHECKS_LIMIT:]
        parts.append(
            f"## Checks output\n\nCommand: `{checks['command']}`, exit code "
            f"{checks['exit_code']}.\n\n```\n{output}\n```\n"
        )
    else:
        parts.append(f"## Checks output\n\nThe checks were not run: {checks.get('reason')}.\n")
    summary = _text(trial.folder / "summary.md") or "(the build wrote no final summary)"
    parts.append(f"## The build's final summary\n\n{summary}\n")
    return "\n".join(parts)


def _call(fn, timeout: float | None, agent: ClaudeCode):
    """Run ``fn`` and give up after ``timeout`` seconds, or once build-bench is
    stopped, leaving it behind."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as error:  # noqa: BLE001
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    give_up = time.time() + timeout if timeout else None
    while thread.is_alive() and (give_up is None or time.time() < give_up):
        if agent.stopped:
            raise JudgeFailed(STOPPED)
        thread.join(0.2)
    if timeout is not None and thread.is_alive():
        raise TimeoutError(f"timeout: no reply after {timeout / 60:g} minutes")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _ask_api(
    settings: Settings, folder: Path, j: Judge, trial: Trial, label: str, agent: ClaudeCode
) -> tuple[str, float | None]:
    import litellm

    limits = settings.judge_limits(j)
    timeout = limits.timeout_minutes * 60 if limits.timeout_minutes else None
    request: dict[str, Any] = {
        "model": j.model,
        "messages": [
            {"role": "system", "content": judge_prompt(j)},
            {"role": "user", "content": evidence(settings, folder, trial, label)},
        ],
    }
    if timeout:
        request["timeout"] = timeout
    if j.api_key_env:
        request["api_key"] = os.environ[j.api_key_env]
    response = _call(lambda: litellm.completion(**request), timeout, agent)
    try:
        reply = response.choices[0].message.content or ""
    except (AttributeError, IndexError, KeyError, TypeError):
        raise JudgeFailed(f"unexpected reply shape: {response!r:.200}") from None
    try:
        cost = float(litellm.completion_cost(completion_response=response))
    except Exception:  # noqa: BLE001 - unknown models have no price
        cost = None
    return reply, cost


def _ask_agent(
    settings: Settings, folder: Path, j: Judge, trial: Trial, label: str, agent: ClaudeCode
) -> tuple[str, float | None]:
    if not trial.workspace.is_dir():
        raise JudgeFailed(
            "the Trial's workspace is gone (removed by clean); an Agent Judge needs it"
        )
    limits = settings.judge_limits(j)
    experiment = read_json(folder / "experiment.json") or {}
    base = experiment.get("base_sha") or settings.base
    with tempfile.TemporaryDirectory(prefix="build-bench-judge-") as tmp:
        work = Path(tmp) / label
        shutil.copytree(trial.workspace, work, symlinks=True)
        _blind_history(work, base)
        config = Path(tmp) / "config"
        config.mkdir()
        checks = (
            f"The project's checks command is `{settings.checks}`."
            if settings.checks
            else "The settings name no checks command; find and run the project's own tests."
        )
        plan = (
            f"The plan is in `{settings.plan_rel}/`."
            if settings.plan_rel
            else "There is no plan folder; the task was given as a prompt."
        )
        summary = _text(trial.folder / "summary.md") or "(the build wrote no final summary)"
        prompt = (
            f"{judge_prompt(j)}\n\n---\n\n# Trial {label}\n\n"
            f"Your working folder is a copy of the build; change it as you like. "
            f"Every change the build made is uncommitted against the base commit "
            f"`{base}`: see `git status` and `git diff {base}`. {FOLDED} {plan} {checks}\n\n"
            f"## The build's final summary\n\n{summary}\n"
        )
        request = AgentRequest(
            prompt=prompt,
            cwd=work,
            config_dir=config,
            model=j.model,
            effort=j.effort,
            session_id=str(uuid.uuid4()),
            stdout_path=trial.folder / "verdicts" / f"{j.name}.stream.jsonl",
            stderr_path=trial.folder / "verdicts" / f"{j.name}.stderr.log",
            auth=(DEFAULT_AUTH_ENV[settings.auth], os.environ[settings.auth_variable]),
            timeout_minutes=limits.timeout_minutes,
            max_budget_usd=limits.max_budget_usd,
        )
        result = agent.start(request)
    if result.failure:
        raise JudgeFailed(result.failure)
    reply = (result.result or {}).get("result") or ""
    cost = (result.result or {}).get("total_cost_usd")
    return reply, float(cost) if cost is not None else None


def _blind_history(repo: Path, base: str) -> None:
    """Leave every change uncommitted on top of the base commit and drop the
    build's commit messages, which can name the model that wrote them."""

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("reset", "--soft", base)
    git("reflog", "expire", "--expire=now", "--all")
    for leftover in ("ORIG_HEAD", "COMMIT_EDITMSG"):
        (repo / ".git" / leftover).unlink(missing_ok=True)
    git("prune", "--expire=now")
