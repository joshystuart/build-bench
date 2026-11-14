"""A stand-in for the `copilot` executable (the GitHub Copilot CLI).

It answers like `tests/fake_claude.py` and reads the same control file, with
`copilot_mode` (ok | crash | hang) for its builds and `copilot_version` for
`--version`. What it prints and the event log it leaves are a real recording
of Copilot CLI 1.0.112, in `fixtures/transcripts/copilot/`.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fake_claude  # noqa: E402
import replies  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "copilot"
DENIED = "Error: Access denied by policy settings (Request ID: 0000)\n\nYour Copilot CLI policy"


def main() -> int:
    control_path = os.environ.get("FAKE_CLAUDE_CONTROL")
    control = json.loads(Path(control_path).read_text()) if control_path else {}
    args = sys.argv[1:]
    if "--version" in args:
        print(f"GitHub Copilot CLI {control.get('copilot_version', '1.0.112')}.")
        print("Run 'copilot update' to check for updates.")
        return 0

    def option(name):
        return args[args.index(name) + 1] if name in args else None

    prompt = option("-p") or ""
    cwd = os.getcwd()
    kind = replies.kind(prompt)
    mode = control.get("copilot_mode", "ok")
    if kind is not None:
        mode = control.get(f"{kind}_mode", control.get("judge_mode", "ok"))
    home = Path(os.environ.get("COPILOT_HOME", "/nonexistent"))
    record = {
        "agent": "copilot",
        "argv": args,
        "cwd": cwd,
        "prompt": prompt,
        "judge": kind is not None,
        "kind": kind,
        "mode": mode,
        "env": {
            k: os.environ.get(k)
            for k in ("COPILOT_HOME", "COPILOT_MODEL", "COPILOT_GITHUB_TOKEN", "ANTHROPIC_API_KEY")
        },
        "config_files": sorted(p.name for p in home.iterdir()) if home.is_dir() else None,
        "start": time.time(),
    }
    log_dir = Path(os.environ["FAKE_CLAUDE_LOG"])
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{uuid.uuid4()}.json").write_text(json.dumps(record))

    session = str(uuid.uuid4())
    if mode == "crash":
        print(DENIED, file=sys.stderr)
        print(json.dumps({"type": "result", "sessionId": session, "exitCode": 1, "usage": {}}))
        return 1
    if mode == "hang":
        time.sleep(600)
    time.sleep(control.get("sleep", 0))

    if kind == "find":
        said = control.get("judge_reply", replies.FOUND)
    elif kind == "merge":
        said = control.get("merge_reply", replies.merge_reply(prompt))
    elif kind == "check":
        said = replies.check_reply(prompt, control.get("check_present", "yes"))
    else:
        fake_claude._tick_plan(cwd)
        fake_claude._build(cwd, option("--model") or "?")
        said = control.get("summary", f"Fake summary: all done. {os.environ.get('GH_TOKEN', '')}")

    state = home / "session-state" / session
    state.mkdir(parents=True)
    (state / "events.jsonl").write_text((FIXTURE / "main.jsonl").read_text())

    events = [json.loads(line) for line in (FIXTURE / "stdout.jsonl").read_text().splitlines()]
    # The main agent's last message is its answer; a subagent's messages carry an id.
    last = [e for e in events if e["type"] == "assistant.message" and not e.get("agentId")][-1]
    last["data"]["content"] = said
    for event in events:
        print(json.dumps(event))
    return 0


if __name__ == "__main__":
    sys.exit(main())
