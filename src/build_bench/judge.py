"""`judge`: measure each finished Trial, then find, merge and check its Defects.

1. Find: each Judge reviews each Trial and lists its Findings (a Verdict).
2. Merge: one Judge groups every Finding worse than a nit into the Experiment's
   distinct Defects, saved in `defects.json`.
3. Check: each Judge says, for each Trial, which of those Defects it has.

Every step skips work already saved.
"""

from __future__ import annotations

import hashlib
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
from .agent import STOPPED, AgentRequest, Agents
from .results import Trial, read_json, saved_trials, write_json
from .run import git, in_parallel, make_workspace
from .settings import Judge, Settings, path_rules

#: A Finding's severity, worst first.
SEVERITIES = ("critical", "major", "minor", "nit")
#: Nits are counted per Trial but never merged into Defects or checked.
MERGED = SEVERITIES[:3]
PRESENT = ("yes", "no", "unsure")
#: Judges are blind, so the build's own commits (whose messages can name the
#: model) are never shown. Say so, or a Judge counts the missing commits against it.
FOLDED = (
    "Whether the build committed its work is hidden from you on purpose: its commits "
    "are folded into these changes. Do not count that against the build."
)
#: Checks output longer than this is cut to its end before a Judge sees it.
CHECKS_LIMIT = 30_000


def judge(settings: Settings, folder: Path, say=print, agent: Agents | None = None) -> bool:
    """Measure, then find, merge and check. Returns False if any step failed."""
    agent = agent or Agents()
    trials = saved_trials(folder)
    for trial in trials:
        measure_trial(settings, folder, trial, say)
    ready = [t for t in trials if t.finished and (t.folder / "measurements.json").exists()]
    found = find(settings, folder, ready, say, agent)
    merged = merge(settings, folder, ready, say, agent)
    checked = check(settings, folder, ready, say, agent)
    return found and merged and checked


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
        return {"ran": False, "reason": "the Trial's workspace is not here (removed by clean?)"}
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


# --- Asking a Judge ------------------------------------------------------------


class VerdictError(Exception):
    """A Judge's reply cannot be used."""


class JudgeFailed(Exception):
    pass


def shipped_prompt(name: str) -> str:
    return (resources.files("build_bench") / "prompts" / f"{name}.md").read_text()


def judge_prompt(j: Judge) -> str:
    return j.prompt.read_text() if j.prompt is not None else shipped_prompt("judge")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _str(value: Any) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else json.dumps(value)


def reply_json(reply: str, key: str) -> dict:
    """The last JSON object in a Judge's reply that holds `key`."""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", reply, flags=re.S)
    decoder = json.JSONDecoder()
    if not candidates:
        for i in [m.start() for m in re.finditer(r"\{", reply)]:
            try:
                value, _ = decoder.raw_decode(reply[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and key in value:
                candidates.append(json.dumps(value))
    for text in reversed(candidates):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and key in value:
            return value
    raise VerdictError(f"the reply holds no JSON object with {key}")


def facts(trial: Trial) -> str:
    """What build-bench has already measured, so no Judge spends time recounting it."""
    measured = read_json(trial.folder / "measurements.json") or {}
    counts = measured.get("lines") or {}
    checks = measured.get("checks") or {}
    if not checks.get("ran"):
        ran = f"The checks were not run: {checks.get('reason')}."
    elif checks.get("passed"):
        ran = f"The checks (`{checks['command']}`) passed."
    elif checks.get("timed_out"):
        ran = f"The checks (`{checks['command']}`) timed out."
    else:
        ran = f"The checks (`{checks['command']}`) failed with exit code {checks['exit_code']}."
    return (
        "## Already measured\n\n"
        f"- Production lines: {counts.get('prod_added', 0)} added, "
        f"{counts.get('prod_removed', 0)} removed. Test lines: "
        f"{counts.get('test_added', 0)} added, {counts.get('test_removed', 0)} removed.\n"
        f"- {ran}\n"
    )


def evidence(settings: Settings, folder: Path, trial: Trial, label: str) -> str:
    """What an API Judge reads: the plan as first written, the diff and the checks output.

    The build's own summary and its changes to the plan are left out: both can
    name the Variant, and a Judge should hold every build to the same plan.
    """
    parts = [f"# Trial {label}\n"]
    plan = folder / "plan"
    if plan.is_dir():
        parts.append(f"## The plan (at `{settings.plan_rel}` in the repository)\n")
        for f in sorted(p for p in plan.rglob("*") if p.is_file()):
            parts.append(f"### {f.relative_to(plan).as_posix()}\n\n{_text(f)}\n")
    else:
        parts.append("## The plan\n\nThere is no plan folder; the task was given as a prompt.\n")
    diff = _text(trial.folder / "diff.patch")
    parts.append(f"## Diff against the base commit\n\n{FOLDED}\n\n```diff\n{diff}```\n")
    parts.append(facts(trial))
    checks = (read_json(trial.folder / "measurements.json") or {}).get("checks") or {}
    if checks.get("ran"):
        output = _text(trial.folder / "checks.log")
        if len(output) > CHECKS_LIMIT:
            output = "[earlier output cut]\n" + output[-CHECKS_LIMIT:]
        parts.append(
            f"## Checks output\n\nCommand: `{checks['command']}`, exit code "
            f"{checks['exit_code']}.\n\n```\n{output}\n```\n"
        )
    return "\n".join(parts)


def _call(fn, timeout: float | None, agent: Agents):
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
    settings: Settings, j: Judge, system: str, user: str, agent: Agents
) -> tuple[str, float | None]:
    import litellm

    limits = settings.judge_limits(j)
    timeout = limits.timeout_minutes * 60 if limits.timeout_minutes else None
    request: dict[str, Any] = {
        "model": j.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
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
    settings: Settings, j: Judge, prompt: str, work: Path, streams: Path, agent: Agents
) -> tuple[str, float | None]:
    """Start an Agent Judge in `work` with an empty config folder beside it.
    `streams` is the path its output files are named after."""
    limits = settings.judge_limits(j)
    config = work.parent / "config"
    config.mkdir()
    streams.parent.mkdir(parents=True, exist_ok=True)
    request = AgentRequest(
        prompt=prompt,
        cwd=work,
        config_dir=config,
        model=j.model,
        effort=j.effort,
        session_id=str(uuid.uuid4()),
        stdout_path=streams.with_name(streams.name + ".stream.jsonl"),
        stderr_path=streams.with_name(streams.name + ".stderr.log"),
        auth=settings.agent_login(j.type),
        timeout_minutes=limits.timeout_minutes,
        max_budget_usd=limits.max_budget_usd,
    )
    result = agent.of(j.type).start(request)
    if result.failure:
        raise JudgeFailed(result.failure)
    return result.summary or "", result.cost_usd


def ask_about_trial(
    settings: Settings,
    folder: Path,
    j: Judge,
    trial: Trial,
    label: str,
    instructions: str,
    extra: str,
    streams: Path,
    agent: Agents,
) -> tuple[str, float | None]:
    """Ask one Judge about one Trial, shown under `label`. `extra` follows the evidence."""
    if j.type == "api":
        user = evidence(settings, folder, trial, label) + extra
        return _ask_api(settings, j, instructions, user, agent)
    experiment = read_json(folder / "experiment.json") or {}
    base = experiment.get("base_sha") or settings.base
    with tempfile.TemporaryDirectory(prefix="build-bench-judge-") as tmp:
        work = Path(tmp) / label
        rebuilt = ""
        if trial.workspace.is_dir():
            shutil.copytree(trial.workspace, work, symlinks=True)
        else:
            _rebuild(settings, folder, trial, work, base)
            rebuilt = (
                " The folder holds only what git tracks: files git ignores (installed "
                "dependencies, build output) are missing, so install what you need "
                "before you run anything."
            )
        _blind_history(work, base)
        if settings.plan_rel and (folder / "plan").is_dir():
            # Every build is held to the plan as first written, not its own edits.
            shutil.rmtree(work / settings.plan_rel, ignore_errors=True)
            shutil.copytree(folder / "plan", work / settings.plan_rel, symlinks=True)
        checks = (
            f"The project's checks command is `{settings.checks}`."
            if settings.checks
            else "The settings name no checks command; find and run the project's own tests."
        )
        plan = (
            f"The plan is in `{settings.plan_rel}/`, as it was before the build started."
            if settings.plan_rel
            else "There is no plan folder; the task was given as a prompt."
        )
        prompt = (
            f"{instructions}\n\n---\n\n# Trial {label}\n\n"
            f"Your working folder is a copy of the build; change it as you like. "
            f"Every change the build made is uncommitted against the base commit "
            f"`{base}`: see `git status` and `git diff {base}`.{rebuilt} {FOLDED} {plan} "
            f"{checks}\n\n"
            f"{facts(trial)}{extra}"
        )
        return _ask_agent(settings, j, prompt, work, streams, agent)


def _rebuild(settings: Settings, folder: Path, trial: Trial, work: Path, base: str) -> None:
    """The build again, from the base commit and the Trial's saved diff, for a Trial
    whose workspace is not here: `clean` removed it, or it was built on another
    machine. Files git ignores are not in the diff, so they are not in the copy."""
    patch = trial.folder / "diff.patch"
    try:
        make_workspace(settings, folder, work, base)
        if _text(patch).strip():
            git(work, "apply", "--index", "--whitespace=nowarn", str(patch))
    except (RuntimeError, OSError) as error:
        raise JudgeFailed(
            f"the Trial's workspace is not here ({trial.workspace}) and could not be "
            f"rebuilt from its saved diff: {error}"
        ) from error


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


def _attempt(record: dict, target: Path, ask, parse) -> dict:
    """Ask, parse the reply into `record` and save it at `target`, whatever happens.
    Cost and time are added to what `record` already holds."""
    started = time.time()
    spent, seconds = record.get("cost_usd"), record.get("seconds") or 0.0
    record.update(status="failed", reason=None)
    try:
        reply, cost = ask()
        if cost is not None:
            record["cost_usd"] = (spent or 0.0) + cost
        target.with_suffix(".reply.md").write_text(reply)
        record.update(parse(reply), status="ok")
    except (VerdictError, JudgeFailed, TimeoutError) as error:
        record["reason"] = str(error) or type(error).__name__
    except Exception as error:  # noqa: BLE001 - a broken call fails this step only
        record["reason"] = f"{type(error).__name__}: {error}"
    record["seconds"] = round(seconds + time.time() - started, 3)
    write_json(target, record)
    return record


def _record(j: Judge, **more: Any) -> dict[str, Any]:
    return {
        "judge": j.name,
        "type": j.type,
        "model": j.model,
        "status": "failed",
        "reason": None,
        "cost_usd": None,
        "seconds": None,
        **more,
    }


def _ok(path: Path) -> bool:
    return (read_json(path) or {}).get("status") == "ok"


# --- Find: one Verdict per Judge and Trial -------------------------------------


def check_findings(value: dict) -> dict:
    """A Verdict's Findings and note, checked against the fixed shape."""
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise VerdictError("findings is not a list")
    problems, clean = [], []
    for n, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            problems.append(f"finding {n} is not an object")
            continue
        title, severity = finding.get("title"), finding.get("severity")
        if not isinstance(title, str) or not title.strip():
            problems.append(f"finding {n} has no title")
        if severity not in SEVERITIES:
            problems.append(
                f"finding {n} has severity {severity!r}, not one of {', '.join(SEVERITIES)}"
            )
        clean.append(
            {
                "title": _str(title),
                "severity": severity,
                "where": _str(finding.get("where")),
                "detail": _str(finding.get("detail")),
                "reproduced": finding.get("reproduced") is True,
            }
        )
    if problems:
        raise VerdictError("; ".join(problems))
    return {"findings": clean, "note": _str(value.get("note"))}


def find(settings: Settings, folder: Path, trials: list[Trial], say, agent: Agents) -> bool:
    """Ask each Judge for a Verdict on each Trial that has none from its current prompt."""
    todo = []
    for j in settings.judges:
        sha = _sha(judge_prompt(j))
        for trial in trials:
            saved = read_json(trial.folder / "verdicts" / f"{j.name}.json") or {}
            if saved.get("status") != "ok" or saved.get("prompt_sha") != sha:
                todo.append((j, trial))
    lock = threading.Lock()

    def one(task: tuple[Judge, Trial]) -> None:
        j, trial = task
        verdict = find_one(settings, folder, j, trial, agent)
        with lock:
            if verdict["status"] == "ok":
                say(f"[verdict] {trial.id} by {j.name}")
            else:
                say(f"[verdict failed] {trial.id} by {j.name}: {verdict['reason']}")

    in_parallel(one, todo, settings.concurrency, agent, say)
    return all(
        _ok(t.folder / "verdicts" / f"{j.name}.json") for j in settings.judges for t in trials
    )


def find_one(settings: Settings, folder: Path, j: Judge, trial: Trial, agent: Agents) -> dict:
    label = f"trial-{secrets.token_hex(3)}"
    instructions = judge_prompt(j)
    target = trial.folder / "verdicts" / f"{j.name}.json"
    target.parent.mkdir(exist_ok=True)
    return _attempt(
        _record(j, label=label, prompt_sha=_sha(instructions)),
        target,
        lambda: ask_about_trial(
            settings, folder, j, trial, label, instructions, "", target.with_suffix(""), agent
        ),
        lambda reply: check_findings(reply_json(reply, "findings")),
    )


# --- Merge: the Experiment's distinct Defects -----------------------------------


def reported(trials: list[Trial]) -> dict[str, dict]:
    """Every Finding worse than a nit in a saved Verdict, by a key that names its
    Trial and Judge and changes when the Finding does."""
    found = {}
    for trial in trials:
        for path in sorted((trial.folder / "verdicts").glob("*.json")):
            verdict = read_json(path) or {}
            if verdict.get("status") != "ok":
                continue
            for finding in verdict.get("findings") or []:
                if finding.get("severity") in MERGED:
                    text = json.dumps([finding["title"], finding["where"], finding["detail"]])
                    found[f"{trial.id}/{path.stem}/{_sha(text)}"] = finding
    return found


def merge_judge(settings: Settings) -> Judge | None:
    if not settings.judges:
        return None
    named = [j for j in settings.judges if j.name == settings.merge_judge]
    return named[0] if named else settings.judges[0]


def merge(settings: Settings, folder: Path, trials: list[Trial], say, agent: Agents) -> bool:
    """Place every Finding not yet in a Defect: into a Defect that already
    exists, which keeps its id, or into a new one."""
    j = merge_judge(settings)
    if j is None:
        return True
    target = folder / "defects.json"
    saved = read_json(target) or {}
    found = reported(trials)
    defects = []
    for defect in saved.get("defects") or []:
        # A Verdict judged again drops its old Findings; a Defect with none left goes too.
        kept = [key for key in defect["findings"] if key in found]
        if kept:
            defects.append({**defect, "findings": kept})
    placed = {key for defect in defects for key in defect["findings"]}
    pending = {f"F{n}": key for n, key in enumerate((k for k in found if k not in placed), 1)}
    record = _record(
        j,
        cost_usd=saved.get("cost_usd"),
        seconds=saved.get("seconds"),
        next_id=saved.get("next_id", 1),
        defects=_with_severity(defects, found),
    )
    if not pending:
        record["status"] = "ok"
        write_json(target, record)
        return True

    def parse(reply: str) -> dict:
        groups = check_groups(reply_json(reply, "defects"), {d["id"] for d in defects}, pending)
        by_id = {d["id"]: d for d in defects}
        number = record["next_id"]
        for group in groups:
            keys = [pending[f] for f in group["findings"]]
            if group["id"] in by_id:
                by_id[group["id"]]["findings"] += keys
                continue
            if group.get("left_out"):
                # The reply skipped this Finding: it stands as a Defect in its own words.
                group = {"title": found[keys[0]]["title"], "detail": found[keys[0]]["detail"]}
            new = {"id": f"D{number}", "title": group["title"], "description": group["detail"]}
            defects.append({**new, "findings": keys})
            number += 1
        return {"defects": _with_severity(defects, found), "next_id": number}

    instructions = shipped_prompt("merge")
    body = merge_request(defects, pending, found)

    def ask() -> tuple[str, float | None]:
        if j.type == "api":
            return _ask_api(settings, j, instructions, body, agent)
        with tempfile.TemporaryDirectory(prefix="build-bench-merge-") as tmp:
            work = Path(tmp) / "merge"
            work.mkdir()
            prompt = f"{instructions}\n\n---\n\n{body}"
            return _ask_agent(settings, j, prompt, work, target.with_suffix(""), agent)

    _attempt(record, target, ask, parse)
    if record["status"] == "ok":
        say(f"[merged] {len(found)} finding(s) into {len(defects)} defect(s) by {j.name}")
    else:
        say(f"[merge failed] by {j.name}: {record['reason']}")
    return record["status"] == "ok"


def _with_severity(defects: list[dict], found: dict[str, dict]) -> list[dict]:
    """Each Defect is as severe as its worst Finding."""
    for defect in defects:
        defect["severity"] = min(
            (found[key]["severity"] for key in defect["findings"]), key=SEVERITIES.index
        )
    return defects


def merge_request(defects: list[dict], pending: dict[str, str], found: dict[str, dict]) -> str:
    parts = ["## Existing defects\n"]
    if not defects:
        parts.append("None yet.\n")
    for defect in defects:
        parts.append(f"### {defect['id']}: {defect['title']}\n\n{defect['description']}\n")
    parts.append("## New findings\n")
    for name, key in pending.items():
        finding = found[key]
        parts.append(
            f"### {name}: {finding['title']}\n\n"
            f"Where: {finding['where'] or 'not given'}\n\n{finding['detail']}\n"
        )
    return "\n".join(parts)


def check_groups(value: dict, existing: set[str], pending: dict[str, str]) -> list[dict]:
    """The merge reply as groups of new Findings, each placed exactly once.

    A Finding named twice stays where it was named first. One the reply leaves
    out becomes a Defect of its own, so no Finding is ever lost.
    """
    groups = value.get("defects")
    if not isinstance(groups, list):
        raise VerdictError("defects is not a list")
    problems, clean = [], []
    seen: set[str] = set()
    for n, group in enumerate(groups, 1):
        if not isinstance(group, dict) or not isinstance(group.get("findings"), list):
            problems.append(f"defect {n} has no list of findings")
            continue
        names = [f for f in dict.fromkeys(map(str, group["findings"])) if f not in seen]
        unknown = [f for f in names if f not in pending]
        if unknown:
            problems.append(f"defect {n} names unknown finding(s) {', '.join(unknown)}")
            continue
        seen.update(names)
        new = group.get("id") not in existing
        if new and not _str(group.get("title")):
            problems.append(f"defect {n} is new but has no title")
        if names:
            clean.append(
                {
                    "id": group.get("id"),
                    "title": _str(group.get("title")),
                    "detail": _str(group.get("description")),
                    "findings": names,
                }
            )
    if problems:
        raise VerdictError("; ".join(problems))
    return clean + [
        {"id": None, "title": name, "detail": "", "findings": [name], "left_out": True}
        for name in pending
        if name not in seen
    ]


# --- Check: which Trials have which Defects ---------------------------------------


def check(settings: Settings, folder: Path, trials: list[Trial], say, agent: Agents) -> bool:
    """Ask each Judge, for each Trial, about every Defect it has not answered for."""
    defects = (read_json(folder / "defects.json") or {}).get("defects") or []
    if not defects:
        return True
    instructions = shipped_prompt("check")
    sha = _sha(instructions)

    def unanswered(j: Judge, trial: Trial) -> tuple[dict, list[dict]]:
        saved = read_json(trial.folder / "defect-checks" / f"{j.name}.json") or {}
        answers = (saved.get("answers") or {}) if saved.get("prompt_sha") == sha else {}
        return saved, [d for d in defects if d["id"] not in answers]

    todo = [(j, t) for j in settings.judges for t in trials if unanswered(j, t)[1]]
    lock = threading.Lock()

    def one(task: tuple[Judge, Trial]) -> None:
        j, trial = task
        saved, missing = unanswered(j, trial)
        label = f"trial-{secrets.token_hex(3)}"
        target = trial.folder / "defect-checks" / f"{j.name}.json"
        target.parent.mkdir(exist_ok=True)
        kept = saved if saved.get("prompt_sha") == sha else {}
        record = _record(
            j,
            label=label,
            prompt_sha=sha,
            cost_usd=kept.get("cost_usd"),
            seconds=kept.get("seconds"),
            answers=dict(kept.get("answers") or {}),
        )
        asked = "## Known defects\n\n" + "\n".join(
            f"### {d['id']}: {d['title']}\n\n{d['description']}\n" for d in missing
        )
        _attempt(
            record,
            target,
            lambda: ask_about_trial(
                settings,
                folder,
                j,
                trial,
                label,
                instructions,
                asked,
                target.with_suffix(""),
                agent,
            ),
            lambda reply: {
                "answers": {
                    **record["answers"],
                    **check_answers(reply_json(reply, "answers"), [d["id"] for d in missing]),
                }
            },
        )
        with lock:
            if record["status"] == "ok":
                say(f"[checked] {trial.id} by {j.name}")
            else:
                say(f"[check failed] {trial.id} by {j.name}: {record['reason']}")

    in_parallel(one, todo, settings.concurrency, agent, say)
    return all(
        _ok(t.folder / "defect-checks" / f"{j.name}.json") and not unanswered(j, t)[1]
        for j in settings.judges
        for t in trials
    )


def check_answers(value: dict, asked: list[str]) -> dict[str, dict]:
    answers = value.get("answers")
    if not isinstance(answers, list):
        raise VerdictError("answers is not a list")
    problems, clean = [], {}
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("id") not in asked:
            continue
        if answer.get("present") not in PRESENT:
            problems.append(
                f"{answer['id']} has present {answer.get('present')!r}, "
                f"not one of {', '.join(PRESENT)}"
            )
        clean[answer["id"]] = {
            "present": answer.get("present"),
            "where": _str(answer.get("where")),
            "evidence": _str(answer.get("evidence")),
        }
    missing = [name for name in asked if name not in clean]
    if missing:
        problems.append(f"no answer for {', '.join(missing)}")
    if problems:
        raise VerdictError("; ".join(problems))
    return clean
