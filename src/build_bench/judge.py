"""`judge`: measure each finished Trial, then find, merge and check its Defects.

1. Find: each Judge reviews each Trial and lists its Findings (a Verdict).
2. Merge: one Judge groups every Finding worse than a nit into the Experiment's
   distinct Defects, saved in `defects.json`.
3. Check: each Judge says, for each Trial, which of those Defects it has and
   how severe each is there.
4. Compare: a Judge with `compare` set says which of two Trials is better
   (see `compare.py`).

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
from functools import partial
from importlib import resources
from pathlib import Path
from typing import Any

from . import lines, transcript
from .agent import STOPPED, AgentRequest, Agents
from .judge_progress import Ask, Board, ask_of
from .progress import clock
from .results import Trial, read_json, saved_trials, write_json
from .run import (
    checks_in,
    checks_output,
    git,
    in_parallel,
    make_workspace,
    run_steps,
    runtime_problem,
    settings_key,
)
from .settings import Judge, Settings, path_rules

#: A Finding's severity, worst first.
SEVERITIES = ("critical", "major", "minor", "nit")
#: Nits are counted per Trial but never merged into Defects or checked.
MERGED = SEVERITIES[:3]
PRESENT = ("yes", "no", "unsure")
#: What kind of problem a Finding or Defect is, in the order the report ranks them.
CATEGORIES = ("behaviour", "test-gap", "scope")
#: Judges are blind, so the build's own commits (whose messages can name the
#: model) are never shown. Say so, or a Judge counts the missing commits against it.
FOLDED = (
    "Whether the build committed its work is hidden from you on purpose: its commits "
    "are folded into these changes. Do not count that against the build."
)
#: Checks output longer than this is cut to its end before a Judge sees it.
CHECKS_LIMIT = 30_000


def judge(
    settings: Settings,
    folder: Path,
    say=print,
    agent: Agents | None = None,
    live: bool = False,
    *,
    fresh: bool = False,
) -> bool:
    """Measure, then find, merge and check. Returns False if any step failed.
    With `fresh`, everything judging saved before is removed first.

    Each step says what it will do as it starts. With `live`, a display of the
    step's asks is redrawn below the printed lines; it needs a terminal.
    """
    from .compare import compare

    agent = agent or Agents(settings.env)
    board = Board(settings, say)
    with board.shown(live):
        trials = saved_trials(folder)
        if fresh:
            board.say(
                "Judging again from nothing (--fresh): measurements, Verdicts, Defects, "
                "Defect checks and Comparisons."
            )
            forget_judging(folder, trials)
        measured = measure(settings, folder, trials, board)
        ready = [
            t
            for t, m in zip(trials, measured, strict=True)
            if t.finished and m is not None and not m.get("error")
        ]
        found = find(settings, folder, ready, board, agent)
        merged = merge(settings, folder, ready, board, agent)
        checked = check(settings, folder, ready, board, agent)
        compared = compare(settings, folder, ready, board, agent)
    unmeasured = any(m and m.get("error") for m in measured)
    ok = found and merged and checked and compared and not unmeasured
    board.say(
        f"[judge] finished in {clock(time.time() - board.started)}"
        + ("" if ok else "; some steps failed (see above), `build-bench judge` tries them again")
    )
    return ok


def measure(
    settings: Settings, folder: Path, trials: list[Trial], board: Board
) -> list[dict | None]:
    """Measure each finished Trial not measured yet, one at a time; return each
    Trial's measurements (None for a failed Trial)."""
    todo = [t for t in trials if t.finished and not (t.folder / "measurements.json").exists()]
    failed = sum(not t.finished for t in trials)
    doing = "counting lines and cost" + (", then running the checks" if settings.checks else "")
    asks = {t.id: Ask(t.id, doing=doing) for t in todo}
    line = f"{len(todo)} Trial(s) to measure, {len(trials) - len(todo) - failed} already measured"
    if failed:
        line += f", {failed} failed and not judged"
    board.step("measure", list(asks.values()), line)
    measured: list[dict | None] = []
    for trial in trials:
        ask = asks.get(trial.id)
        if ask is None:
            measured.append(measure_trial(settings, folder, trial, board.say))
            continue

        def one(t: Trial = trial) -> dict:
            m = measure_trial(settings, folder, t, board.say) or {}
            measured.append(m)
            if m.get("error"):
                return {"status": "failed", "reason": m["error"]}
            return {"status": "ok"}

        board.asking(ask, one)
    return measured


def forget_judging(folder: Path, trials: list[Trial]) -> None:
    """Remove what `judge` saved in a results folder, so every step runs again
    and the Defect ids start from the first. A measurement whose workspace could
    not be put back is kept: measuring it again would take the damage for the build.
    So is one whose workspace is gone, as its checks could not run again."""
    for trial in trials:
        for name in ("verdicts", "defect-checks"):
            shutil.rmtree(trial.folder / name, ignore_errors=True)
        saved = read_json(trial.folder / "measurements.json") or {}
        if saved.get("error") or not trial.workspace.is_dir():
            continue
        for name in ("measurements.json", "checks.log"):
            (trial.folder / name).unlink(missing_ok=True)
        shutil.rmtree(trial.folder / "checks", ignore_errors=True)
    for path in folder.glob("defects.*"):
        path.unlink()
    shutil.rmtree(folder / "comparisons", ignore_errors=True)


# --- Automatic measurements -------------------------------------------------

#: The settings besides the Checks that the checks' result depends on.
CHECKS_SETTINGS = ["env", "copy_setup", "runtime_check", "checks_in_place"]


def checks_stale(settings: Settings, saved: dict) -> bool:
    """Whether the checks saved in a measurement ran under other settings than
    these. A measurement from before this was recorded is kept while those
    settings are at their defaults and the checks command is the same."""
    key = saved.get("checks_key")
    if key is not None:
        return key != settings_key(settings, CHECKS_SETTINGS)
    command = (saved.get("checks") or {}).get("command")
    changed = command is not None and command != settings.checks_command
    return changed or any(
        (settings.env, settings.copy_setup, settings.runtime_check, settings.checks_in_place)
    )


def measure_trial(settings: Settings, folder: Path, trial: Trial, say=print) -> dict | None:
    target = trial.folder / "measurements.json"
    if not trial.finished:
        say(f"[skip]  {trial.id}: the Trial failed, so it is not measured")
        return None
    if target.exists():
        saved = read_json(target)
        if saved.get("error"):
            say(f"[measure failed] {trial.id}: {saved['error']}")
        elif trial.workspace.is_dir() and (
            (not_run := bool(settings.checks) and (saved.get("checks") or {}).get("ran") is False)
            or checks_stale(settings, saved)
        ):
            why = "they did not run last time" if not_run else "the settings they ran under changed"
            say(f"[checks again] {trial.id}: {why}")
            (trial.folder / "checks.log").unlink(missing_ok=True)
            shutil.rmtree(trial.folder / "checks", ignore_errors=True)
            _measure_checks(settings, trial, saved)
            write_json(target, saved)
            if saved.get("error"):
                say(f"[measure failed] {trial.id}: {saved['error']}")
        return saved

    prices = {**transcript.default_prices(), **settings.prices}
    measured: dict[str, Any] = {}
    try:
        measured.update(transcript.measure(trial.folder / "transcript", prices))
    except (FileNotFoundError, ValueError) as error:
        measured["transcript_error"] = str(error)

    test_paths, ignore_paths, rust_inline = path_rules(settings)
    diff = _text(trial.folder / "diff-full.patch") or _text(trial.folder / "diff.patch")
    measured["lines"] = lines.count_lines(diff, test_paths, ignore_paths, rust_inline)
    _measure_checks(settings, trial, measured)
    write_json(target, measured)
    if measured.get("error"):
        say(f"[measure failed] {trial.id}: {measured['error']}")
    else:
        say(f"[measured] {trial.id}")
    return measured


def _measure_checks(settings: Settings, trial: Trial, measured: dict) -> None:
    """Run the checks into `measured`, with the settings they ran under."""
    try:
        measured["checks"] = run_checks(settings, trial)
    except RestoreFailed as error:
        # The workspace no longer holds what the build left, so judging it would
        # judge the checks. It stays failed until it is measured from nothing.
        measured["error"] = str(error)
    measured["checks_key"] = settings_key(settings, CHECKS_SETTINGS)


def _text(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() else ""


def run_checks(settings: Settings, trial: Trial) -> dict:
    """Run `checks` in a throwaway copy, so the saved workspace never changes, or
    with `checks_in_place` in the workspace itself."""
    if not settings.checks:
        return {"ran": False, "reason": "the settings name no checks command"}
    if not trial.workspace.is_dir():
        return {"ran": False, "reason": "the Trial's workspace is not here (removed by clean?)"}
    if settings.checks_in_place:
        with tempfile.TemporaryDirectory(prefix="build-bench-snapshot-") as tmp:
            index = Path(tmp) / "index"
            try:
                before = _snapshot(trial.workspace, index)
            except (RuntimeError, OSError) as error:
                return {"ran": False, "reason": f"the workspace could not be saved: {error}"}
            try:
                with (trial.folder / "checks.log").open("w") as out:
                    failure = runtime_problem(settings, trial.workspace, out)
                if not failure:
                    result = _checks_with_logs(settings, trial, trial.workspace)
            finally:
                # Even when stopped halfway, so the next `judge` never takes what
                # the checks left for the build.
                _put_back(trial.workspace, index, before)
        return {"ran": False, "reason": failure} if failure else result
    log = trial.folder / "checks.log"
    with tempfile.TemporaryDirectory(prefix="build-bench-checks-") as tmp:
        copy = Path(tmp) / "trial"
        shutil.copytree(trial.workspace, copy, symlinks=True)
        with log.open("w") as out:
            failure = set_up_copy(settings, trial, copy, out) or runtime_problem(
                settings, copy, out
            )
        if failure:
            return {"ran": False, "reason": failure}
        return _checks_with_logs(settings, trial, copy)


def _checks_with_logs(settings: Settings, trial: Trial, cwd: Path) -> dict:
    """Run the Checks in `cwd`; add their output to `checks.log`, and save each
    Check's own in `checks/<name>.log`."""
    result, outputs = checks_in(settings, cwd)
    with (trial.folder / "checks.log").open("a") as out:
        out.write(checks_output(settings, outputs))
    logs = trial.folder / "checks"
    logs.mkdir(exist_ok=True)
    for name, text in outputs.items():
        (logs / f"{name}.log").write_text(text)
    return result


#: The variable that tells `copy_setup` where the Trial's own workspace is.
WORKSPACE_VARIABLE = "BUILD_BENCH_WORKSPACE"


def set_up_copy(settings: Settings, trial: Trial, copy: Path, out) -> str | None:
    """Run `copy_setup` in a throwaway copy of `trial`. Returns why it failed, if it did."""
    env = {**settings.environment(), WORKSPACE_VARIABLE: str(trial.workspace)}
    return run_steps("copy_setup", settings.copy_setup, copy, out, env=env)


class RestoreFailed(Exception):
    """The workspace could not be put back as it was before the checks."""


def _snapshot(repo: Path, index: Path) -> str:
    """Every file git tracks or could track in `repo`, as a tree saved in its object
    store, staged in the separate `index`. The real index is copied beside it."""
    real = repo / ".git" / "index"
    if real.exists():
        shutil.copyfile(real, index)
        shutil.copyfile(real, index.with_name("real-index"))
    git(repo, "add", "--all", env={"GIT_INDEX_FILE": str(index)})
    return git(repo, "write-tree", env={"GIT_INDEX_FILE": str(index)}).strip()


def _put_back(repo: Path, index: Path, tree: str) -> None:
    """Put every tracked and untracked file back as `_snapshot` saw it and remove
    the untracked files added since. Files git ignores are left alone."""
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        git(repo, "read-tree", tree, env=env)
        git(repo, "checkout-index", "--all", "--force", env=env)
        git(repo, "clean", "--force", "-d", "--quiet", env=env)
        real = index.with_name("real-index")
        if real.exists():
            shutil.copyfile(real, repo / ".git" / "index")
        git(repo, "add", "--all", env=env)
        after = git(repo, "write-tree", env=env).strip()
    except (RuntimeError, OSError) as error:
        raise RestoreFailed(
            f"the workspace could not be put back after the checks: {error}"
        ) from error
    if after != tree:
        raise RestoreFailed(
            "the workspace could not be put back after the checks: its files still "
            f"differ from before them (see `git status` in {repo})"
        )


# --- Asking a Judge ------------------------------------------------------------


class VerdictError(Exception):
    """A Judge's reply cannot be used."""


class JudgeFailed(Exception):
    pass


#: The heading the Experiment's `judge_guidance` is added under.
GUIDANCE_HEADING = "## Guidance for this Experiment"


def with_guidance(settings: Settings, prompt: str) -> str:
    """`prompt` with the Experiment's guidance added under its own heading. With no
    guidance it is unchanged, so its hash, and every result saved with it, holds."""
    if settings.judge_guidance is None:
        return prompt
    return f"{prompt.rstrip()}\n\n{GUIDANCE_HEADING}\n\n{settings.judge_guidance.read_text()}"


def shipped_prompt(settings: Settings, name: str) -> str:
    """A shipped judging prompt, with the Experiment's guidance."""
    shipped = (resources.files("build_bench") / "prompts" / f"{name}.md").read_text()
    return with_guidance(settings, shipped)


def judge_prompt(settings: Settings, j: Judge) -> str:
    """The find prompt: the Judge's own, or the shipped one, with the guidance."""
    if j.prompt is not None:
        return with_guidance(settings, j.prompt.read_text())
    return shipped_prompt(settings, "judge")


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
    each = checks.get("checks") or []
    if not checks.get("ran"):
        ran = f"The checks were not run: {checks.get('reason')}."
    elif len(each) > 1 or (each and each[0]["name"] != "checks"):
        ran = "\n- ".join(_outcome(f"The check `{c['name']}` (`{c['command']}`)", c) for c in each)
    else:
        ran = _outcome(f"The checks (`{checks['command']}`)", checks)
    return (
        "## Already measured\n\n"
        f"- Production lines: {counts.get('prod_added', 0)} added, "
        f"{counts.get('prod_removed', 0)} removed. Test lines: "
        f"{counts.get('test_added', 0)} added, {counts.get('test_removed', 0)} removed.\n"
        f"- {ran}\n"
    )


def _outcome(what: str, result: dict) -> str:
    if result.get("passed"):
        return f"{what} passed."
    if result.get("timed_out"):
        return f"{what} timed out."
    return f"{what} failed with exit code {result['exit_code']}."


def evidence(settings: Settings, folder: Path, trial: Trial, label: str) -> str:
    """What an API Judge reads: the plan as first written, the diff and the checks output.

    The build's own summary and its changes to the plan are left out: both can
    name the Variant, and a Judge should hold every build to the same plan.
    """
    return "\n".join([f"# Trial {label}\n", *plan_parts(settings, folder), *trial_parts(trial)])


def plan_parts(settings: Settings, folder: Path) -> list[str]:
    plan = folder / "plan"
    if not plan.is_dir():
        return ["## The plan\n\nThere is no plan folder; the task was given as a prompt.\n"]
    parts = [f"## The plan (at `{settings.plan_rel}` in the repository)\n"]
    for f in sorted(p for p in plan.rglob("*") if p.is_file()):
        parts.append(f"### {f.relative_to(plan).as_posix()}\n\n{_text(f)}\n")
    return parts


def trial_parts(trial: Trial) -> list[str]:
    """One Trial's diff, measurements and checks output."""
    diff = _text(trial.folder / "diff.patch")
    parts = [f"## Diff against the base commit\n\n{FOLDED}\n\n```diff\n{diff}```\n", facts(trial)]
    checks = (read_json(trial.folder / "measurements.json") or {}).get("checks") or {}
    if not checks.get("ran"):
        return parts
    each = checks.get("checks")
    if not each:
        # Saved before each Check had its own log.
        output = _cut(_text(trial.folder / "checks.log"))
        parts.append(
            f"## Checks output\n\nCommand: `{checks['command']}`, exit code "
            f"{checks['exit_code']}.\n\n```\n{output}\n```\n"
        )
        return parts
    parts.append("## Checks output\n")
    for c in each:
        output = _cut(_text(trial.folder / "checks" / f"{c['name']}.log"))
        parts.append(
            f"### {c['name']}\n\nCommand: `{c['command']}`, exit code {c['exit_code']}."
            f"\n\n```\n{output}\n```\n"
        )
    return parts


def _cut(output: str) -> str:
    """Long output cut to its end, where the failures are usually summed up."""
    if len(output) > CHECKS_LIMIT:
        return "[earlier output cut]\n" + output[-CHECKS_LIMIT:]
    return output


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
    return reply, _api_cost(response)


def _api_cost(response: Any) -> float | None:
    """What one API call cost: from LiteLLM's price list, else what the provider
    itself reported with the reply (OpenRouter does), else unknown."""
    import litellm

    try:
        return float(litellm.completion_cost(completion_response=response))
    except Exception:  # noqa: BLE001 - LiteLLM's price list does not know every model
        pass
    reported = getattr(getattr(response, "usage", None), "cost", None)
    return float(reported) if isinstance(reported, int | float) else None


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
    base = base_commit(settings, folder)
    with tempfile.TemporaryDirectory(prefix="build-bench-judge-") as tmp:
        work = Path(tmp) / label
        rebuilt = blind_copy(settings, folder, trial, work, base, _setup_log(streams))
        prompt = (
            f"{instructions}\n\n---\n\n# Trial {label}\n\n"
            f"Your working folder is a copy of the build; change it as you like. "
            f"Every change the build made is uncommitted against the base commit "
            f"`{base}`: see `git status` and `git diff {base}`.{rebuilt} {FOLDED} "
            f"{where_things_are(settings)}\n\n"
            f"{facts(trial)}{extra}"
        )
        return _ask_agent(settings, j, prompt, work, streams, agent)


def base_commit(settings: Settings, folder: Path) -> str:
    experiment = read_json(folder / "experiment.json") or {}
    return experiment.get("base_sha") or settings.base


def where_things_are(settings: Settings) -> str:
    """Where an Agent Judge finds the plan, and the checks command."""
    named = settings.named_checks
    if settings.one_checks_command:
        checks = f"The project's checks command is `{settings.checks_command}`."
    elif named:
        listed = [f"`{c.name}` (`{c.command}`)" for c in named]
        joined = ", ".join(listed[:-1]) + " and " + listed[-1] if len(listed) > 1 else listed[0]
        checks = f"The project's checks are {joined}."
    else:
        checks = "The settings name no checks command; find and run the project's own tests."
    plan = (
        f"The plan is in `{settings.plan_rel}/`, as it was before the build started."
        if settings.plan_rel
        else "There is no plan folder; the task was given as a prompt."
    )
    return f"{plan} {checks}"


def _setup_log(streams: Path) -> Path:
    """Where `copy_setup` output goes for a Judge whose own output is named after `streams`."""
    return streams.with_name(streams.name + ".copy_setup.log")


def blind_copy(
    settings: Settings, folder: Path, trial: Trial, work: Path, base: str, setup_log: Path
) -> str:
    """Put a copy of the Trial's build at `work` for an Agent Judge: every change
    uncommitted on the base commit, and the plan as first written, then run
    `copy_setup` there, its output in `setup_log`. Returns what to tell the Judge
    when the copy had to be rebuilt, else nothing."""
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
    if settings.copy_setup:
        setup_log.parent.mkdir(parents=True, exist_ok=True)
        with setup_log.open("w") as out:
            failure = set_up_copy(settings, trial, work, out)
        if failure:
            # A Judge in a copy that is not set up would review the setup, not the build.
            raise JudgeFailed(failure)
    return rebuilt


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
        if _stops_judge(error):
            record["stops_judge"] = True
    record["seconds"] = round(seconds + time.time() - started, 3)
    write_json(target, record)
    return record


def _stops_judge(error: Exception) -> bool:
    """An API Judge's error that every later ask would get too: no login, no
    permission, or no money left."""
    import litellm

    return isinstance(error, litellm.BudgetExceededError) or getattr(
        error, "status_code", None
    ) in (401, 402, 403)


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
        category = finding.get("category")
        if not isinstance(title, str) or not title.strip():
            problems.append(f"finding {n} has no title")
        if severity not in SEVERITIES:
            problems.append(
                f"finding {n} has severity {severity!r}, not one of {', '.join(SEVERITIES)}"
            )
        if category not in CATEGORIES:
            problems.append(
                f"finding {n} has category {category!r}, not one of {', '.join(CATEGORIES)}"
            )
        clean.append(
            {
                "title": _str(title),
                "severity": severity,
                "category": category,
                "where": _str(finding.get("where")),
                "detail": _str(finding.get("detail")),
                "reproduced": finding.get("reproduced") is True,
            }
        )
    if problems:
        raise VerdictError("; ".join(problems))
    return {"findings": clean, "note": _str(value.get("note"))}


def find(
    settings: Settings, folder: Path, trials: list[Trial], board: Board, agent: Agents
) -> bool:
    """Ask each Judge for a Verdict on each Trial that has none from its current prompt."""
    todo = []
    for j in settings.judges:
        sha = _sha(judge_prompt(settings, j))
        for trial in trials:
            path = trial.folder / "verdicts" / f"{j.name}.json"
            saved = read_json(path) or {}
            if saved.get("status") != "ok" or saved.get("prompt_sha") != sha:
                todo.append((j, trial, ask_of(f"{trial.id} by {j.name}", j, path.with_suffix(""))))
    total = len(settings.judges) * len(trials)
    if not settings.judges:
        line = "no Judges in the settings"
    elif not todo:
        line = f"all {total} Verdict(s) already saved"
    else:
        line = (
            f"{len(todo)} Verdict(s) to ask for, {total - len(todo)} already saved "
            f"({len(settings.judges)} Judge(s) × {len(trials)} Trial(s), "
            f"{settings.concurrency} at a time)"
        )
    board.step("find", [ask for *_, ask in todo], line)
    lock = threading.Lock()

    def one(task: tuple[Judge, Trial, Ask]) -> None:
        j, trial, ask = task
        verdict = board.asking(ask, lambda: find_one(settings, folder, j, trial, agent))
        with lock:
            if verdict["status"] == "ok":
                board.say(f"[verdict] {trial.id} by {j.name}")
            elif verdict["status"] == "failed":
                board.say(f"[verdict failed] {trial.id} by {j.name}: {verdict['reason']}")

    in_parallel(one, todo, settings.concurrency, agent, board.say)
    return all(
        _ok(t.folder / "verdicts" / f"{j.name}.json") for j in settings.judges for t in trials
    )


def find_one(settings: Settings, folder: Path, j: Judge, trial: Trial, agent: Agents) -> dict:
    label = f"trial-{secrets.token_hex(3)}"
    instructions = judge_prompt(settings, j)
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


def merge(
    settings: Settings, folder: Path, trials: list[Trial], board: Board, agent: Agents
) -> bool:
    """Place every Finding not yet in a Defect: into a Defect that already
    exists, which keeps its id, or into a new one."""
    j = merge_judge(settings)
    if j is None:
        board.step("merge", [], "no Judges in the settings")
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
        board.step("merge", [], f"no new Findings to merge ({len(defects)} Defect(s) so far)")
        record["status"] = "ok"
        write_json(target, record)
        return True
    # Another Judge tries when the merge Judge fails, so one Judge's outage does
    # not leave every Defect count unknown.
    others = [other for other in settings.judges if other is not j]
    ask = ask_of(f"merge by {j.name}", j, target.with_suffix(""))
    board.step(
        "merge",
        [ask],
        f"{len(pending)} new Finding(s) to place among {len(defects)} Defect(s), by {j.name}"
        + (", or the next Judge if it fails" if others else ""),
    )

    def parse(reply: str) -> dict:
        categories = {name: found[key].get("category") for name, key in pending.items()}
        existing = {d["id"]: d.get("category") for d in defects}
        groups = check_groups(reply_json(reply, "defects"), existing, categories)
        by_id = {d["id"]: d for d in defects}
        number = record["next_id"]
        for group in groups:
            keys = [pending[f] for f in group["findings"]]
            if group["id"] in by_id:
                by_id[group["id"]]["findings"] += keys
                continue
            if group.get("left_out"):
                # The reply skipped this Finding: it stands as a Defect in its own words.
                first = found[keys[0]]
                group = {**group, "title": first["title"], "detail": first["detail"]}
            new = {"id": f"D{number}", "title": group["title"], "description": group["detail"]}
            category = group["category"] or most_common([categories[f] for f in group["findings"]])
            defects.append({**new, "category": category, "findings": keys})
            number += 1
        return {"defects": _with_severity(defects, found), "next_id": number}

    instructions = shipped_prompt(settings, "merge")
    body = merge_request(defects, pending, found)

    def call(j: Judge) -> tuple[str, float | None]:
        if j.type == "api":
            return _ask_api(settings, j, instructions, body, agent)
        with tempfile.TemporaryDirectory(prefix="build-bench-merge-") as tmp:
            work = Path(tmp) / "merge"
            work.mkdir()
            prompt = f"{instructions}\n\n---\n\n{body}"
            return _ask_agent(settings, j, prompt, work, target.with_suffix(""), agent)

    def attempt(judge: Judge, record: dict) -> dict:
        return _attempt(record, target, lambda: call(judge), parse)

    failed: list[dict] = []
    for n, judge in enumerate([j, *others]):
        if n:
            if judge.name in board.stopped:
                continue
            ask = ask_of(f"merge by {judge.name}", judge, target.with_suffix(""))
            board.add(ask)
            board.say(f"[merge] trying again with {judge.name}")
            record = {**_record(judge), **{k: record[k] for k in ("next_id", "defects")}}
        # Each failed try is kept, so its cost and reason count for its own Judge.
        record["failed_tries"] = failed
        done = board.asking(ask, partial(attempt, judge, record))
        if done["status"] == "ok":
            board.say(
                f"[merged] {len(found)} finding(s) into {len(defects)} defect(s) by {judge.name}"
            )
            return True
        if done["status"] == "failed":
            board.say(f"[merge failed] by {judge.name}: {done['reason']}")
            failed.append({k: done.get(k) for k in ("judge", "reason", "cost_usd", "seconds")})
    if not failed:
        # Every Judge was stopped before its turn: the Defects saved before are
        # missing these Findings, so they must not read as merged.
        record.update(status="failed", reason="every Judge was stopped before it could merge")
        write_json(target, record)
    # Otherwise the last failed try is saved, holding the tries before it.
    return False


def _with_severity(defects: list[dict], found: dict[str, dict]) -> list[dict]:
    """Each Defect starts as severe as its worst Finding. Once the Judges have
    checked for it, the report uses the severity they voted instead."""
    for defect in defects:
        defect["severity"] = min(
            (found[key]["severity"] for key in defect["findings"]), key=SEVERITIES.index
        )
    return defects


def most_common(categories: list[str | None]) -> str | None:
    """The category most of these have; on a tie, the one ranked first."""
    known = [c for c in categories if c in CATEGORIES]
    if not known:
        return None
    return max(CATEGORIES, key=lambda c: (known.count(c), -CATEGORIES.index(c)))


def _category_line(category: str | None) -> str:
    return f"Category: {category}. " if category else ""


def merge_request(defects: list[dict], pending: dict[str, str], found: dict[str, dict]) -> str:
    parts = ["## Existing defects\n"]
    if not defects:
        parts.append("None yet.\n")
    for defect in defects:
        parts.append(
            f"### {defect['id']}: {defect['title']}\n\n"
            f"{_category_line(defect.get('category'))}{defect['description']}\n"
        )
    parts.append("## New findings\n")
    for name, key in pending.items():
        finding = found[key]
        parts.append(
            f"### {name}: {finding['title']}\n\n{_category_line(finding.get('category'))}"
            f"Where: {finding['where'] or 'not given'}\n\n{finding['detail']}\n"
        )
    return "\n".join(parts)


def check_groups(
    value: dict, existing: dict[str, str | None], pending: dict[str, str | None]
) -> list[dict]:
    """The merge reply as groups of new Findings, each placed exactly once.
    `existing` gives each Defect's category and `pending` each new Finding's.

    A Finding named twice stays where it was named first. One the reply leaves
    out becomes a Defect of its own, so no Finding is ever lost. Findings of
    different categories never share a Defect.
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
        category = group.get("category") if new else existing[group["id"]]
        if new and category is not None and category not in CATEGORIES:
            problems.append(
                f"defect {n} has category {category!r}, not one of {', '.join(CATEGORIES)}"
            )
        mixed = {pending[f] for f in names} | ({category} if category in CATEGORIES else set())
        mixed.discard(None)
        if len(mixed) > 1:
            listed = ", ".join(c for c in CATEGORIES if c in mixed)
            problems.append(f"defect {n} mixes findings of different categories: {listed}")
        if names:
            clean.append(
                {
                    "id": group.get("id"),
                    "title": _str(group.get("title")),
                    "detail": _str(group.get("description")),
                    "category": category if new else None,
                    "findings": names,
                }
            )
    if problems:
        raise VerdictError("; ".join(problems))
    return clean + [
        {
            "id": None,
            "title": name,
            "detail": "",
            "category": pending[name],
            "findings": [name],
            "left_out": True,
        }
        for name in pending
        if name not in seen
    ]


# --- Check: which Trials have which Defects ---------------------------------------


def check(
    settings: Settings, folder: Path, trials: list[Trial], board: Board, agent: Agents
) -> bool:
    """Ask each Judge, for each Trial, about every Defect it has not answered for."""
    defects = (read_json(folder / "defects.json") or {}).get("defects") or []
    if not defects:
        board.step("check", [], "no Defects to check for")
        return True
    instructions = shipped_prompt(settings, "check")
    sha = _sha(instructions)

    def unanswered(j: Judge, trial: Trial) -> tuple[dict, list[dict]]:
        saved = read_json(trial.folder / "defect-checks" / f"{j.name}.json") or {}
        answers = (saved.get("answers") or {}) if saved.get("prompt_sha") == sha else {}
        return saved, [d for d in defects if d["id"] not in answers]

    def path(j: Judge, trial: Trial) -> Path:
        return trial.folder / "defect-checks" / f"{j.name}.json"

    todo = [
        (j, t, ask_of(f"{t.id} by {j.name}", j, path(j, t).with_suffix("")))
        for j in settings.judges
        for t in trials
        if unanswered(j, t)[1]
    ]
    total = len(settings.judges) * len(trials)
    if not todo:
        line = f"every Trial already checked for all {len(defects)} Defect(s)"
    else:
        line = (
            f"{len(todo)} of {total} Judge × Trial ask(s) about {len(defects)} Defect(s), "
            f"{settings.concurrency} at a time"
        )
    board.step("check", [ask for *_, ask in todo], line)
    lock = threading.Lock()

    def one(task: tuple[Judge, Trial, Ask]) -> None:
        j, trial, ask = task
        saved, missing = unanswered(j, trial)
        label = f"trial-{secrets.token_hex(3)}"
        target = path(j, trial)
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

        def call() -> tuple[str, float | None]:
            streams = target.with_suffix("")
            return ask_about_trial(
                settings, folder, j, trial, label, instructions, asked, streams, agent
            )

        def parse(reply: str) -> dict:
            got = check_answers(reply_json(reply, "answers"), [d["id"] for d in missing])
            return {"answers": {**record["answers"], **got}}

        done = board.asking(ask, lambda: _attempt(record, target, call, parse))
        with lock:
            if done["status"] == "ok":
                board.say(f"[checked] {trial.id} by {j.name}")
            elif done["status"] == "failed":
                board.say(f"[check failed] {trial.id} by {j.name}: {done['reason']}")

    in_parallel(one, todo, settings.concurrency, agent, board.say)
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
        severity = answer.get("severity") if answer.get("present") == "yes" else None
        if answer.get("present") == "yes" and severity not in MERGED:
            problems.append(
                f"{answer['id']} is present with severity {severity!r}, "
                f"not one of {', '.join(MERGED)}"
            )
        in_base = answer.get("in_base") if answer.get("present") == "yes" else None
        if answer.get("present") == "yes" and in_base not in PRESENT:
            problems.append(
                f"{answer['id']} has in_base {in_base!r}, not one of {', '.join(PRESENT)}"
            )
        clean[answer["id"]] = {
            "present": answer.get("present"),
            "severity": severity,
            "in_base": in_base,
            "where": _str(answer.get("where")),
            "evidence": _str(answer.get("evidence")),
        }
    missing = [name for name in asked if name not in clean]
    if missing:
        problems.append(f"no answer for {', '.join(missing)}")
    if problems:
        raise VerdictError("; ".join(problems))
    return clean
