"""Start one headless coding agent. Only Claude Code is implemented.

How Claude Code is started (flags, environment, where transcripts land) is
recorded in docs/headless-trials.md; keep the two in step.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class AgentRequest:
    prompt: str
    cwd: Path
    config_dir: Path
    model: str
    session_id: str
    stdout_path: Path
    stderr_path: Path
    #: The login: one variable name and its value, set for the agent alone.
    auth: tuple[str, str]
    effort: str | None = None
    timeout_minutes: float | None = None
    max_budget_usd: float | None = None


@dataclass
class AgentResult:
    seconds: float
    started_at: float
    ended_at: float
    #: The agent's final ``result`` event, if it wrote one.
    result: dict | None = None
    #: Why the agent failed, or ``None`` when it finished.
    failure: str | None = None


#: Why an agent ended when build-bench itself was stopped.
STOPPED = "stopped: build-bench was stopped (Ctrl+C)"


class Agent(Protocol):
    def version(self) -> str: ...

    def start(self, request: AgentRequest) -> AgentResult: ...

    def stop(self, now: bool = False) -> None: ...

    @property
    def stopped(self) -> bool: ...

    def transcript(self, request: AgentRequest) -> tuple[Path | None, Path | None]: ...


def _scrubbed_environment() -> dict[str, str]:
    """This process's environment without anything that would tie the agent to the
    session that started build-bench: nested-session markers, an inherited config
    folder, effort or login."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE") and k not in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    }


class ClaudeCode:
    def __init__(self, executable: str = "claude"):
        self.executable = executable
        self._stopping = threading.Event()
        self._hurry = threading.Event()

    def stop(self, now: bool = False) -> None:
        """Start no more agents and stop the running ones: politely, or with `now`
        at once. Each `start` still returns, saying it was stopped."""
        self._stopping.set()
        if now:
            self._hurry.set()

    @property
    def stopped(self) -> bool:
        return self._stopping.is_set()

    def _path(self) -> str:
        found = shutil.which(self.executable)
        if found is None:
            raise FileNotFoundError(f"{self.executable!r} is not on the PATH")
        return found

    def version(self) -> str:
        out = subprocess.run(
            [self._path(), "--version"],
            capture_output=True,
            text=True,
            env=_scrubbed_environment(),
            timeout=60,
        )
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError(f"`claude --version` failed: {out.stderr.strip()}")
        return out.stdout.split()[0]

    def command(self, request: AgentRequest) -> list[str]:
        cmd = [
            self._path(),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            request.model,
            "--permission-mode",
            "bypassPermissions",
            "--session-id",
            request.session_id,
        ]
        if request.effort:
            cmd += ["--effort", request.effort]
        if request.max_budget_usd:
            cmd += ["--max-budget-usd", str(request.max_budget_usd)]
        return cmd

    def environment(self, request: AgentRequest) -> dict[str, str]:
        env = _scrubbed_environment()
        env["CLAUDE_CONFIG_DIR"] = str(request.config_dir)
        name, value = request.auth
        # The agent reads its login from its usual variable, whatever the
        # settings file called the developer's variable.
        env[name] = value
        return env

    def start(self, request: AgentRequest) -> AgentResult:
        started = time.time()
        if self.stopped:
            return AgentResult(seconds=0, started_at=started, ended_at=started, failure=STOPPED)
        deadline = started + request.timeout_minutes * 60 if request.timeout_minutes else None
        timed_out = False
        with request.stdout_path.open("wb") as out, request.stderr_path.open("wb") as err:
            process = subprocess.Popen(
                self.command(request),
                cwd=request.cwd,
                env=self.environment(request),
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                assert process.stdin is not None
                process.stdin.write(request.prompt.encode())
                process.stdin.close()
            except BrokenPipeError:
                pass
            # The agent runs in its own session, so a Ctrl+C never reaches it;
            # `stop` is how it is stopped instead.
            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self.stopped:
                    _stop(process, self._hurry)
                    break
                if deadline is not None and time.time() >= deadline:
                    timed_out = True
                    _stop(process, self._hurry)
                    break
        ended = time.time()
        result = _last_result(request.stdout_path)
        failure = _failure(process.returncode, timed_out, request, result)
        return AgentResult(
            seconds=ended - started,
            started_at=started,
            ended_at=ended,
            result=result,
            failure=STOPPED if failure and self.stopped else failure,
        )

    def transcript(self, request: AgentRequest) -> tuple[Path | None, Path | None]:
        """The main transcript and the folder of subagent transcripts, if present."""
        for main in (request.config_dir / "projects").glob(f"*/{request.session_id}.jsonl"):
            subagents = main.with_suffix("") / "subagents"
            return main, subagents if subagents.is_dir() else None
        return None, None


def _stop(process: subprocess.Popen, hurry: threading.Event) -> None:
    """Ask the agent's process group to end; kill it after 10 seconds, or as soon
    as `hurry` is set."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        give_up = time.time() + 10
        while time.time() < give_up and (sig == signal.SIGKILL or not hurry.is_set()):
            try:
                process.wait(timeout=0.2)
                return
            except subprocess.TimeoutExpired:
                continue


def _last_result(stdout_path: Path) -> dict | None:
    result = None
    for line in stdout_path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


def _failure(
    code: int | None, timed_out: bool, request: AgentRequest, result: dict | None
) -> str | None:
    if timed_out:
        return f"timeout: stopped after {request.timeout_minutes:g} minutes"
    if result is not None and result.get("subtype") == "error_max_budget_usd":
        return f"budget: reached the spending limit of ${request.max_budget_usd or 0:g}"
    if result is not None and (result.get("is_error") or result.get("subtype") != "success"):
        detail = result.get("result") or "; ".join(result.get("errors") or []) or "no detail"
        return f"error: {result.get('subtype')}: {detail}"
    if code != 0:
        tail = request.stderr_path.read_text(errors="replace").strip().splitlines()[-5:]
        return f"crashed: exit code {code}" + (f": {' | '.join(tail)}" if tail else "")
    if result is None:
        return "crashed: the agent wrote no result"
    return None
