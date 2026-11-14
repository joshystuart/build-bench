"""Start one headless coding agent: Claude Code or the GitHub Copilot CLI.

How each is started (flags, environment, where transcripts land) is recorded
in docs/headless-trials.md; keep the two in step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


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
    #: None when the agent finds its own login (the Copilot CLI).
    auth: tuple[str, str] | None = None
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
    #: The agent's last message, if it wrote one.
    summary: str | None = None
    #: What the agent says the run cost, when it says.
    cost_usd: float | None = None


#: Why an agent ended when build-bench itself was stopped.
STOPPED = "stopped: build-bench was stopped (Ctrl+C)"


class Headless:
    """What every agent shares: starting the process, the timeout and stopping."""

    #: The executable's usual name, and what to call the agent in messages.
    executable = ""
    name = ""

    def __init__(self, executable: str | None = None, stopping: Stopping | None = None):
        self.executable = executable or self.executable
        self._stop = stopping or Stopping()

    def stop(self, now: bool = False) -> None:
        """Start no more agents and stop the running ones: politely, or with `now`
        at once. Each `start` still returns, saying it was stopped."""
        self._stop.stop(now)

    @property
    def stopped(self) -> bool:
        return self._stop.stopped

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
            env=self.scrubbed(),
            timeout=60,
        )
        found = re.search(r"\d+(?:\.\d+)+", out.stdout)
        if out.returncode != 0 or found is None:
            raise RuntimeError(f"`{self.executable} --version` failed: {out.stderr.strip()}")
        return found.group()

    def scrubbed(self) -> dict[str, str]:
        raise NotImplementedError

    def command(self, request: AgentRequest) -> list[str]:
        raise NotImplementedError

    def environment(self, request: AgentRequest) -> dict[str, str]:
        raise NotImplementedError

    def stdin(self, request: AgentRequest) -> bytes:
        return b""

    def finish(self, request: AgentRequest, code: int | None, timed_out: bool) -> AgentResult:
        """Read what the agent left behind into a result with no times yet."""
        raise NotImplementedError

    def transcript(self, request: AgentRequest) -> tuple[Path | None, Path | None]:
        raise NotImplementedError

    def start(self, request: AgentRequest) -> AgentResult:
        started = time.time()
        if self.stopped:
            return AgentResult(seconds=0, started_at=started, ended_at=started, failure=STOPPED)
        self._path()
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
                process.stdin.write(self.stdin(request))
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
                    _stop(process, self._stop.hurry)
                    break
                if deadline is not None and time.time() >= deadline:
                    timed_out = True
                    _stop(process, self._stop.hurry)
                    break
        ended = time.time()
        result = self.finish(request, process.returncode, timed_out)
        result.seconds, result.started_at, result.ended_at = ended - started, started, ended
        if result.failure and self.stopped:
            result.failure = STOPPED
        return result


class Stopping:
    """One stop for every agent: Ctrl+C ends Claude Code and Copilot runs alike."""

    def __init__(self) -> None:
        self._stopping = threading.Event()
        self.hurry = threading.Event()

    def stop(self, now: bool = False) -> None:
        self._stopping.set()
        if now:
            self.hurry.set()

    @property
    def stopped(self) -> bool:
        return self._stopping.is_set()


class ClaudeCode(Headless):
    executable = "claude"
    name = "Claude Code"

    def scrubbed(self) -> dict[str, str]:
        """This process's environment without anything that would tie the agent to the
        session that started build-bench: nested-session markers, an inherited config
        folder, effort or login."""
        return {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CLAUDE") and k not in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
        }

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
        env = self.scrubbed()
        env["CLAUDE_CONFIG_DIR"] = str(request.config_dir)
        assert request.auth is not None
        name, value = request.auth
        # The agent reads its login from its usual variable, whatever the
        # settings file called the developer's variable.
        env[name] = value
        return env

    def stdin(self, request: AgentRequest) -> bytes:
        return request.prompt.encode()

    def finish(self, request: AgentRequest, code: int | None, timed_out: bool) -> AgentResult:
        result = _last_result(request.stdout_path)
        found = result or {}
        cost = found.get("total_cost_usd")
        return AgentResult(
            0,
            0,
            0,
            result=result,
            failure=_failure(code, timed_out, request, result),
            summary=found["result"] if isinstance(found.get("result"), str) else None,
            cost_usd=float(cost) if cost is not None else None,
        )

    def transcript(self, request: AgentRequest) -> tuple[Path | None, Path | None]:
        """The main transcript and the folder of subagent transcripts, if present."""
        for main in (request.config_dir / "projects").glob(f"*/{request.session_id}.jsonl"):
            subagents = main.with_suffix("") / "subagents"
            return main, subagents if subagents.is_dir() else None
        return None, None


#: US dollars for one of Copilot's nano AI units: an AI credit is one cent.
USD_PER_NANO_AIU = 0.01 / 1_000_000_000
#: Linux takes at most 128 KB in one command-line argument; a longer prompt goes in a file.
LONGEST_ARGUMENT = 100_000
#: Variables that carry a GitHub login to the Copilot CLI, which it may keep.
COPILOT_LOGIN = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


class Copilot(Headless):
    """The GitHub Copilot CLI. It finds its own login: one of `COPILOT_LOGIN`, the
    system keychain, or the `gh` command's login."""

    executable = "copilot"
    name = "Copilot CLI"

    def scrubbed(self) -> dict[str, str]:
        """This process's environment without Copilot settings that would follow the
        agent in (its home folder, a default model, permissions), keeping its login."""
        return {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("COPILOT_") or k == "COPILOT_GITHUB_TOKEN"
        }

    def command(self, request: AgentRequest) -> list[str]:
        prompt = request.prompt
        if len(prompt.encode()) > LONGEST_ARGUMENT:
            request.config_dir.mkdir(parents=True, exist_ok=True)
            file = request.config_dir / "prompt.md"
            file.write_text(prompt)
            prompt = f"Read the file {file} in full and do what it says."
        cmd = [
            self._path(),
            "-p",
            prompt,
            "--allow-all",
            "--no-ask-user",
            "--no-auto-update",
            "--output-format",
            "json",
            "--model",
            request.model,
        ]
        if request.effort:
            cmd += ["--effort", request.effort]
        return cmd

    def environment(self, request: AgentRequest) -> dict[str, str]:
        env = self.scrubbed()
        env["COPILOT_HOME"] = str(request.config_dir)
        return env

    def finish(self, request: AgentRequest, code: int | None, timed_out: bool) -> AgentResult:
        result = _last_result(request.stdout_path)
        events = _events(request.stdout_path)
        said = [
            e["data"].get("content")
            for e in events
            # A subagent's messages carry its id; the main agent's carry none.
            if e.get("type") == "assistant.message" and not e.get("agentId")
        ]
        summary = next((text for text in reversed(said) if isinstance(text, str) and text), None)
        credits = [
            e["data"].get("totalNanoAiu")
            for e in events
            if e.get("type") == "session.usage_checkpoint"
        ]
        if timed_out:
            failure: str | None = f"timeout: stopped after {request.timeout_minutes:g} minutes"
        elif code != 0 or (result or {}).get("exitCode") not in (0, None):
            tail = request.stderr_path.read_text(errors="replace").strip().splitlines()[:3]
            failure = f"error: exit code {code}" + (f": {' | '.join(tail)}" if tail else "")
        elif result is None:
            failure = "crashed: the agent wrote no result"
        else:
            failure = None
        return AgentResult(
            0,
            0,
            0,
            result=result,
            failure=failure,
            summary=summary,
            cost_usd=credits[-1] * USD_PER_NANO_AIU if credits and credits[-1] else None,
        )

    def transcript(self, request: AgentRequest) -> tuple[Path | None, Path | None]:
        """The session's event log, which holds the subagents' events too. The
        config folder is fresh for each run, so the newest session is this one."""
        found = sorted(
            (request.config_dir / "session-state").glob("*/events.jsonl"),
            key=lambda p: p.stat().st_mtime,
        )
        return (found[-1], None) if found else (None, None)


class Agents:
    """Both agents behind one stop."""

    def __init__(self) -> None:
        self._stop = Stopping()
        self.by_kind: dict[str, Headless] = {
            "claude": ClaudeCode(stopping=self._stop),
            "copilot": Copilot(stopping=self._stop),
        }

    def of(self, kind: str) -> Headless:
        return self.by_kind[kind]

    def stop(self, now: bool = False) -> None:
        self._stop.stop(now)

    @property
    def stopped(self) -> bool:
        return self._stop.stopped


def _events(path: Path) -> list[dict]:
    found = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("data"), dict):
            found.append(event)
    return found


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
