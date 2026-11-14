"""The compare step of `judge`: which of two Trials is better.

A Judge with `compare` set is shown two Trials of different Variants, as build 1
and build 2, and says which it would rather ship, or that they are the same.
Counting Defects misses small differences that a direct comparison picks up.

The pairs are spread evenly over the Variants and their Trials, and which
Variant is shown first alternates, so a Judge that favours one position favours
no Variant. Each Comparison is saved in `comparisons/<judge>/`.
"""

from __future__ import annotations

import tempfile
import threading
from itertools import combinations
from pathlib import Path

from .agent import Agents
from .judge import (
    FOLDED,
    VerdictError,
    _ask_agent,
    _ask_api,
    _attempt,
    _record,
    _sha,
    _str,
    base_commit,
    blind_copy,
    facts,
    plan_parts,
    reply_json,
    shipped_prompt,
    trial_parts,
    where_things_are,
)
from .results import Trial, read_json
from .run import in_parallel
from .settings import Judge, Settings

CHOICES = ("1", "2", "same")


def pairs(trials: list[Trial], most: int) -> list[tuple[Trial, Trial]]:
    """Up to `most` pairs of Trials from different Variants, each in the order shown.

    Pairs are taken in turn from every pair of Variants, and within one from
    Trials not yet used before any is used twice. A larger `most` keeps the
    pairs a smaller one chose and adds to them.
    """
    by_variant: dict[str, list[Trial]] = {}
    for trial in trials:
        by_variant.setdefault(trial.variant, []).append(trial)
    queues = []
    for a, b in combinations(by_variant.values(), 2):
        queue: list[tuple[Trial, Trial]] = []
        for shift in range(len(b)):
            for i in range(max(len(a), len(b))):
                pair = (a[i % len(a)], b[(i + shift) % len(b)])
                if pair not in queue:
                    queue.append(pair)
        # Which Variant is shown first alternates.
        queues.append([pair if n % 2 == 0 else pair[::-1] for n, pair in enumerate(queue)])
    chosen: list[tuple[Trial, Trial]] = []
    turn = 0
    while len(chosen) < most and any(turn < len(q) for q in queues):
        for queue in queues:
            if turn < len(queue) and len(chosen) < most:
                chosen.append(queue[turn])
        turn += 1
    return chosen


def target(folder: Path, j: Judge, first: Trial, second: Trial) -> Path:
    one, two = sorted([first.id, second.id])
    return folder / "comparisons" / j.name / f"{one}__{two}.json"


def compare(settings: Settings, folder: Path, trials: list[Trial], say, agent: Agents) -> bool:
    """Ask each Judge with `compare` set about each of its pairs not yet compared."""
    instructions = shipped_prompt("compare")
    sha = _sha(instructions)
    wanted = [
        (j, first, second)
        for j in settings.judges
        if j.compare is not None
        for first, second in pairs(trials, j.compare.max_pairs)
    ]

    def done(task: tuple[Judge, Trial, Trial]) -> bool:
        saved = read_json(target(folder, *task)) or {}
        return saved.get("status") == "ok" and saved.get("prompt_sha") == sha

    lock = threading.Lock()

    def one(task: tuple[Judge, Trial, Trial]) -> None:
        j, first, second = task
        path = target(folder, j, first, second)
        path.parent.mkdir(parents=True, exist_ok=True)

        def parse(reply: str) -> dict:
            value = reply_json(reply, "better")
            choice = str(value.get("better")).strip().lower()
            if choice not in CHOICES:
                raise VerdictError(
                    f"better is {value.get('better')!r}, not one of {', '.join(CHOICES)}"
                )
            better = {"1": first.id, "2": second.id, "same": None}[choice]
            # `reason` is why a step failed, in every saved record; the Judge's own is `why`.
            return {"better": better, "why": _str(value.get("reason"))}

        record = _attempt(
            _record(j, first=first.id, second=second.id, prompt_sha=sha, better=None, why=""),
            path,
            lambda: _ask(settings, folder, j, first, second, instructions, path, agent),
            parse,
        )
        with lock:
            if record["status"] == "ok":
                say(f"[compared] {first.id} with {second.id} by {j.name}")
            else:
                say(f"[compare failed] {first.id} with {second.id} by {j.name}: {record['reason']}")

    in_parallel(one, [t for t in wanted if not done(t)], settings.concurrency, agent, say)
    return all(done(task) for task in wanted)


def _ask(
    settings: Settings,
    folder: Path,
    j: Judge,
    first: Trial,
    second: Trial,
    instructions: str,
    path: Path,
    agent: Agents,
) -> tuple[str, float | None]:
    builds = list(enumerate((first, second), 1))
    if j.type == "api":
        parts = plan_parts(settings, folder)
        for n, trial in builds:
            parts += [f"# Build {n}\n", *trial_parts(trial)]
        return _ask_api(settings, j, instructions, "\n".join(parts), agent)
    base = base_commit(settings, folder)
    with tempfile.TemporaryDirectory(prefix="build-bench-compare-") as tmp:
        work = Path(tmp) / "builds"
        work.mkdir()
        rebuilt = ""
        for n, trial in builds:
            rebuilt = blind_copy(settings, folder, trial, work / f"build-{n}", base) or rebuilt
        measured = "\n".join(f"# Build {n}\n\n{facts(trial)}" for n, trial in builds)
        prompt = (
            f"{instructions}\n\n---\n\n"
            f"Your working folder holds a copy of each build, in `build-1/` and `build-2/`; "
            f"change them as you like. In each, every change the build made is uncommitted "
            f"against the base commit `{base}`: see `git status` and `git diff {base}` "
            f"there.{rebuilt} {FOLDED} {where_things_are(settings)}\n\n{measured}"
        )
        return _ask_agent(settings, j, prompt, work, path.with_suffix(""), agent)
