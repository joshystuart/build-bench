"""A stand-in for the `claude` executable, driven by a JSON control file.

Control keys (all optional):
    version       what `--version` reports
    mode          ok | crash | hang | stubborn (hang, ignoring SIGTERM) | budget | fail_once
    per_cwd       {substring of the working folder: mode}
    sleep         seconds to wait before answering
    events        stream events a build writes before it waits (to watch it mid-run)
    summary       the final summary a build reports
    judge_reply   the text a judge replies with when asked for a Verdict
    merge_reply   the text a judge replies with when asked to merge Findings
    check_present yes | no | unsure: what a judge answers for every Defect
    compare_better 1 | 2 | same: which build a judge prefers in every Comparison
    judge_mode    mode for judge runs
    merge_mode    mode for merge runs (default: judge_mode)
    check_mode    mode for defect-check runs (default: judge_mode)

Every call is recorded as one JSON file in $FAKE_CLAUDE_LOG.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import replies  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "subagents"


def main() -> int:
    control_path = os.environ.get("FAKE_CLAUDE_CONTROL")
    control = json.loads(Path(control_path).read_text()) if control_path else {}
    args = sys.argv[1:]
    if "--version" in args:
        print(f"{control.get('version', '2.1.319')} (Claude Code)")
        return 0

    def option(name):
        return args[args.index(name) + 1] if name in args else None

    prompt = sys.stdin.read()
    cwd = os.getcwd()
    kind = replies.kind(prompt)
    judge = kind is not None
    mode = control.get("mode", "ok")
    if judge:
        mode = control.get(f"{kind}_mode", control.get("judge_mode", "ok"))
    for key, value in control.get("per_cwd", {}).items():
        if key in cwd and not judge:
            mode = value
    log_dir = Path(os.environ["FAKE_CLAUDE_LOG"])
    log_dir.mkdir(parents=True, exist_ok=True)
    if mode == "fail_once":
        marker = log_dir / ("failed-once-" + Path(cwd).parent.parent.name)
        mode = "ok" if marker.exists() else "crash"
        marker.touch()

    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/nonexistent"))
    record = {
        "argv": args,
        "cwd": cwd,
        "prompt": prompt,
        "judge": judge,
        "kind": kind,
        "mode": mode,
        "env": {
            k: os.environ.get(k)
            for k in (
                "CLAUDE_CONFIG_DIR",
                "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDECODE",
                "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_EFFORT",
            )
        },
        "config_files": sorted(p.name for p in config.iterdir()) if config.is_dir() else None,
        "cwd_files": sorted(
            p.relative_to(cwd).as_posix()
            for p in Path(cwd).rglob("*")
            if ".git" not in p.relative_to(cwd).parts
        ),
        "plan_text": "".join(
            p.read_text()
            for p in sorted(Path(cwd).rglob("*.md"))
            if ".git" not in p.relative_to(cwd).parts
        ),
        "git_log": subprocess.run(
            ["git", "log", "--all", "--format=%B"], capture_output=True, text=True
        ).stdout,
        "git_reflog": subprocess.run(
            ["git", "reflog", "--all"], capture_output=True, text=True
        ).stdout,
        "start": time.time(),
    }
    log_file = log_dir / f"{uuid.uuid4()}.json"
    log_file.write_text(json.dumps(record))

    if mode == "crash":
        print("fake claude crashed on purpose", file=sys.stderr)
        return 3
    if not judge:
        # Tick the plan's first box, and say what it is doing, before waiting,
        # as an agent working through a plan would.
        _tick_plan(cwd)
        for event in control.get("events", []):
            print(json.dumps(event), flush=True)
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode in ("hang", "stubborn"):
        time.sleep(600)
    time.sleep(control.get("sleep", 0))

    model = option("--model") or "?"
    if judge:
        Path(cwd, "judge-was-here.txt").write_text("the judge edits its copy\n")
        Path(cwd, "fake_committed.txt").write_text("changed by the judge\n")
    else:
        _build(cwd, model)

    session = option("--session-id") or str(uuid.uuid4())
    project = config / "projects" / "-fake-project"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE / "main.jsonl", project / f"{session}.jsonl")
    shutil.copytree(FIXTURE / "subagents", project / session / "subagents")

    print(json.dumps({"type": "system", "subtype": "init", "model": model}))
    if mode == "budget":
        result = {
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "errors": ["Reached maximum budget"],
            "total_cost_usd": 0.5,
        }
    else:
        reply = None
        if kind == "find":
            reply = control.get("judge_reply", replies.FOUND)
        elif kind == "merge":
            reply = control.get("merge_reply", replies.merge_reply(prompt))
        elif kind == "check":
            reply = replies.check_reply(prompt, control.get("check_present", "yes"))
        elif kind == "compare":
            reply = replies.compare_reply(control.get("compare_better", "1"))
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": reply if judge else control.get("summary", "Fake summary: all done."),
            "total_cost_usd": 0.0123,
        }
    print(json.dumps(result))
    record["end"] = time.time()
    record["cwd_files_after"] = sorted(
        p.relative_to(cwd).as_posix()
        for p in Path(cwd).rglob("*")
        if ".git" not in p.relative_to(cwd).parts
    )
    log_file.write_text(json.dumps(record))
    return 0


def _build(cwd: str, model: str) -> None:
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=Fake", "-c", "user.email=fake@example.com", *args],
            check=True,
            capture_output=True,
        )

    Path(cwd, "fake_committed.txt").write_text("committed by the fake agent\n")
    git("add", "fake_committed.txt")
    git("commit", "-q", "-m", f"fake commit by {model}")
    Path(cwd, "fake_uncommitted.txt").write_text("left uncommitted\n")


def _tick_plan(cwd: str) -> None:
    for plan in sorted(Path(cwd).rglob("*.md")):
        if ".git" in plan.parts:
            continue
        text = plan.read_text()
        if "- [ ]" in text:
            plan.write_text(text.replace("- [ ]", "- [x]", 1))
            break


if __name__ == "__main__":
    sys.exit(main())
