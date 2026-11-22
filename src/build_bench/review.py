"""The code each Trial produced, with the Judges' Findings pinned on it.

Read only from what every Trial saves in the results folder: `diff-full.patch`
(whole files on both sides), or `diff.patch` for Trials saved before that was
kept. So it works after `clean` has removed the workspaces, and on old results.

`page_data` works out everything `diffs.html` shows: the file matrix, each
Trial's diff of each file with line numbers, which lines to show by default,
and where each Finding and Defect check answer goes. `trial_diff` is the
`diff` command: one Trial's final files against another's.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .judge import finding_key
from .lines import FileDiff, matches, parse
from .report import settled_defects, standing
from .results import ResultsError, Trial, read_json, saved_trials
from .settings import Settings, path_rules

#: Lines of context the page shows around each change and each pinned line.
CONTEXT = 3
FULL, PLAIN = "diff-full.patch", "diff.patch"


def patch(trial: Trial) -> tuple[str | None, str]:
    """The Trial's diff and which file it came from: the whole-file one, or the
    plain one when that is missing or empty (results saved before it was kept)."""
    for name in (FULL, PLAIN):
        path = trial.folder / name
        if path.is_file():
            text = path.read_text(errors="replace")
            if text.strip() or name == PLAIN:
                return name, text
    return None, ""


def whole(f: FileDiff, source: str | None) -> bool:
    """Whether the diff holds every line of the file on both sides."""
    return source == FULL and not f.binary and len(f.starts) <= 1


def trial_state(trial: Trial, source: str | None) -> str:
    if not trial.finished:
        return "failed"
    if trial.changed_nothing:
        return "changed nothing"
    return "built" if source else "no diff saved"


def _file(f: FileDiff, source: str | None) -> dict[str, Any]:
    return {
        "added": sum(1 for mark, _ in f.lines if mark == "+"),
        "removed": sum(1 for mark, _ in f.lines if mark == "-"),
        "binary": f.binary,
        "status": f.status,
        "old_path": f.old_path,
        "whole": whole(f, source),
        "lines": [
            [mark, old, new, text]
            for (mark, text), (old, new) in zip(f.lines, f.numbers, strict=True)
        ],
        "starts": f.starts,
        "shown": [],
        "pins": [],
        "trimmed": False,
    }


# --- Where a Finding is -----------------------------------------------------------

_LOCATION = re.compile(r"^(?P<path>[^:#]+)(?:(?::|#L)(?P<line>\d+)(?:[-–]L?(?P<end>\d+))?)?")
_LINE_WORD = re.compile(r"^(?:L)?(?P<line>\d+)(?:[-–]L?(?P<end>\d+))?$")
_TRIM = "`'\"()[]{}<>,;"


def locations(where: str, paths: list[str]) -> list[tuple[str, int | None, int | None]]:
    """Every (path, line, end line) a `where` names among the changed `paths`.

    Takes `src/a.rs:120`, `src/a.rs:120-130`, `src/a.rs#L120`, `a.rs, line 120`
    and several of these in one string, in backticks or among other words. A
    path matches a changed file by its end, so a Judge may give a shorter or a
    longer path than the diff's."""
    words = re.sub(r"[`,;]", " ", where).split()
    found: list[tuple[str, int | None, int | None]] = []
    for n, word in enumerate(words):
        word = word.strip(_TRIM).rstrip(".:")
        named = _LOCATION.match(word)
        if not named:
            continue
        path = _match(named["path"].rstrip("."), paths)
        if path is None:
            continue
        line, end = named["line"], named["end"]
        if line is None and len(words) > n + 2 and words[n + 1].lower() in ("line", "lines"):
            after = _LINE_WORD.match(words[n + 2].strip(_TRIM).rstrip(".:"))
            if after:
                line, end = after["line"], after["end"]
        place = (path, int(line) if line else None, int(end) if end else None)
        if place not in found:
            found.append(place)
    return found


def _match(given: str, paths: list[str]) -> str | None:
    """The changed path a Judge's path names: the same, or one it ends with, or
    the only one that ends with it."""
    given = given.removeprefix("./")
    if given in paths:
        return given
    if "/" not in given and "." not in given:
        return None
    longer = [p for p in paths if given.endswith("/" + p)]
    if longer:
        return max(longer, key=len)
    shorter = [p for p in paths if p.endswith("/" + given)]
    return shorter[0] if len(shorter) == 1 else None


def _at(entry: dict, line: int | None) -> int | None:
    """The index in the file's diff of a final-file line number, if it is shown."""
    if line is None:
        return None
    return next(
        (i for i, row in enumerate(entry["lines"]) if row[2] == line and row[0] != "-"), None
    )


def _shown(entry: dict, keep: list[int]) -> list[list[int]]:
    """The runs of lines to show by default: each change and each pinned line,
    with CONTEXT lines either side. A run never crosses from one hunk to the next."""
    rows = entry["lines"]
    near: set[int] = set()
    for i in keep + [i for i, row in enumerate(rows) if row[0] != " "]:
        near.update(range(max(0, i - CONTEXT), min(len(rows), i + CONTEXT + 1)))
    near |= _lone_gaps(near, len(rows))
    starts = set(entry["starts"])
    runs: list[list[int]] = []
    for i in sorted(near):
        if runs and runs[-1][1] == i and i not in starts:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    return runs


def _lone_gaps(near: set[int], count: int) -> set[int]:
    """Lines that would be hidden alone: showing one is shorter than saying it is hidden."""
    return {
        i
        for i in range(count)
        if i not in near and (i == 0 or i - 1 in near) and (i == count - 1 or i + 1 in near)
    }


def _trim(entry: dict, pins: list[dict]) -> None:
    """Keep only the lines shown by default. For the files the ignore globs match
    (lockfiles, mostly), whose whole text would be most of the page's size."""
    runs = entry["shown"]
    if not entry["whole"] or sum(end - start for start, end in runs) == len(entry["lines"]):
        return
    moved: dict[int, int] = {}
    lines: list[list]
    starts: list[int]
    shown: list[list[int]]
    lines, starts, shown = [], [], []
    for start, end in runs:
        starts.append(len(lines))
        moved.update({i: len(lines) + i - start for i in range(start, end)})
        lines += entry["lines"][start:end]
        shown.append([starts[-1], len(lines)])
    entry.update(lines=lines, starts=starts, shown=shown, whole=False, trimmed=True)
    for pin in pins:
        for key in ("at", "end_at"):
            if pin[key] is not None:
                pin[key] = moved[pin[key]]


@dataclass
class _Pins:
    """Every pin on the page, and the Trials' files they are pinned to."""

    trials: dict[str, dict]
    pins: list[dict] = field(default_factory=list)

    def add(self, trial: str, where: str, **about: Any) -> None:
        files = self.trials[trial]["files"]
        places = locations(where, list(files))
        if not places:
            self._one(trial, where, None, None, None, about)
        for path, line, end in places:
            self._one(trial, where, path, line, end, about)

    def _one(self, trial, where, path, line, end, about) -> None:
        entry = self.trials[trial]["files"].get(path) if path else None
        pin = {
            "id": len(self.pins),
            "trial": trial,
            "where": where,
            "path": path,
            "line": line,
            "end": end,
            "at": _at(entry, line) if entry else None,
            "end_at": _at(entry, end) if entry else None,
            **about,
        }
        self.pins.append(pin)
        if entry is None:
            self.trials[trial]["unplaced"].append(pin["id"])
        else:
            entry["pins"].append(pin["id"])


def _place_findings(
    trials: list[Trial], judges: list[str], defects: list[dict], into: _Pins
) -> None:
    """Pin every Finding of every saved Verdict, and every Defect check answer
    that says the Trial has the Defect."""
    merged_into = {key: d["id"] for d in defects for key in d["findings"]}
    by_id = {d["id"]: d for d in defects}
    for trial in trials:
        for judge in judges:
            verdict = read_json(trial.folder / "verdicts" / f"{judge}.json") or {}
            if verdict.get("status") == "ok":
                for finding in verdict.get("findings") or []:
                    key = finding_key(trial.id, judge, finding)
                    into.add(
                        trial.id,
                        finding.get("where") or "",
                        judge=judge,
                        kind="Finding",
                        severity=finding.get("severity"),
                        category=finding.get("category"),
                        title=finding.get("title") or "",
                        detail=finding.get("detail") or "",
                        defect=merged_into.get(key),
                        group=merged_into.get(key, key),
                    )
            checked = read_json(trial.folder / "defect-checks" / f"{judge}.json") or {}
            for defect_id, answer in (checked.get("answers") or {}).items():
                defect = by_id.get(defect_id)
                if answer.get("present") != "yes" or defect is None:
                    continue
                into.add(
                    trial.id,
                    answer.get("where") or "",
                    judge=judge,
                    kind="Defect check",
                    severity=answer.get("severity"),
                    category=defect["category"],
                    title=defect["title"],
                    detail=answer.get("evidence") or "",
                    defect=defect_id,
                    group=defect_id,
                )


def _defect_list(
    trials: list[Trial], judges: list[str], defects: list[dict], pins: list[dict]
) -> list[dict]:
    """Each Defect, worst first, with every Trial that has it or had it found:
    which Judges found it, which confirmed it, and its pins there."""
    checks = {
        (trial.id, judge): (read_json(trial.folder / "defect-checks" / f"{judge}.json") or {}).get(
            "answers"
        )
        or {}
        for trial in trials
        for judge in judges
    }
    out = []
    for defect in defects:
        found_by: dict[str, list[str]] = {}
        for key in defect["findings"]:
            trial, judge, _ = key.split("/", 2)
            if judge in judges:
                found_by.setdefault(trial, []).append(judge)
        rows = []
        for trial in trials:
            answers = {}
            for judge in judges:
                answer = checks[trial.id, judge].get(defect["id"])
                if answer:
                    answers[judge] = answer.get("present")
            confirmed = [j for j, a in answers.items() if a == "yes"]
            if not confirmed and trial.id not in found_by:
                continue
            rows.append(
                {
                    "trial": trial.id,
                    "standing": standing(list(answers.values())),
                    "found_by": sorted(set(found_by.get(trial.id, []))),
                    "confirmed_by": confirmed,
                    "denied_by": [j for j, a in answers.items() if a == "no"],
                    "pins": [
                        p["id"]
                        for p in pins
                        if p["trial"] == trial.id and p["defect"] == defect["id"]
                    ],
                }
            )
        out.append(
            {
                **{k: defect[k] for k in ("id", "title", "description", "severity", "category")},
                "in_base": defect["in_base"],
                "trials": rows,
            }
        )
    return out


def _matrix(trials: dict[str, dict], pins: list[dict], ignore: list[str]) -> list[dict]:
    """One row per changed file, the files the fewest Trials touched first. Each
    cell counts the distinct Defects, and Findings not merged into one, pinned there."""
    rows = []
    for path in sorted({p for trial in trials.values() for p in trial["files"]}):
        cells = {}
        for tid, trial in trials.items():
            entry = trial["files"].get(path)
            if entry is not None:
                groups = {pins[i]["group"] for i in entry["pins"]}
                cells[tid] = {"added": entry["added"], "removed": entry["removed"]}
                cells[tid]["pins"] = len(groups)
        rows.append({"path": path, "ignored": matches(path, ignore), "cells": cells})
    return sorted(rows, key=lambda r: (len(r["cells"]), r["path"]))


def page_data(settings: Settings, folder: Path) -> dict[str, Any]:
    """Everything diffs.html shows, worked out here so the page only displays it."""
    judges = [j.name for j in settings.judges]
    found = saved_trials(folder)
    order = [v.name for v in settings.variants]
    order += sorted({t.variant for t in found} - set(order))
    found.sort(key=lambda t: (order.index(t.variant), t.repeat))
    _, ignore, _ = path_rules(settings)

    trials: dict[str, dict] = {}
    for t in found:
        source, text = patch(t)
        state = trial_state(t, source)
        files = {f.path: _file(f, source) for f in parse(text)} if state == "built" else {}
        trials[t.id] = {
            "variant": t.variant,
            "state": state,
            "source": source if state == "built" else None,
            "files": files,
            "unplaced": [],
        }

    merged = read_json(folder / "defects.json") or {}
    defects = settled_defects(merged, found, judges) if merged.get("defects") else []
    pins = _Pins(trials)
    _place_findings(found, judges, defects, pins)
    for entry in (e for trial in trials.values() for e in trial["files"].values()):
        keep = []
        for pin in (pins.pins[i] for i in entry["pins"]):
            keep += [i for i in (pin["at"], pin["end_at"]) if i is not None]
        entry["shown"] = _shown(entry, keep)
    for trial in trials.values():
        for path, entry in trial["files"].items():
            if matches(path, ignore):
                _trim(entry, [pins.pins[i] for i in entry["pins"]])

    return {
        "experiment": (read_json(folder / "experiment.json") or {}).get("name", settings.name),
        "variants": [
            {"name": v, "trials": [tid for tid, t in trials.items() if t["variant"] == v]}
            for v in order
            if any(t["variant"] == v for t in trials.values())
        ],
        "trials": trials,
        "rows": _matrix(trials, pins.pins, ignore),
        "pins": pins.pins,
        "defects": _defect_list(found, judges, defects, pins.pins),
        "merge_failed": bool(merged) and merged.get("status") != "ok",
        "context": CONTEXT,
    }


# --- One Trial against another -------------------------------------------------------


@dataclass
class _Tree:
    """A Trial's final version of each file it changed (None when it removed the
    file), and the base version of each file it saw (None when there was none)."""

    files: dict[str, list[str] | None] = field(default_factory=dict)
    base: dict[str, list[str] | None] = field(default_factory=dict)
    #: Files whose content the diff does not hold: binary, or only partly saved.
    unknown: dict[str, str] = field(default_factory=dict)
    #: Renamed without a change: the new path's content is the old path's base.
    same_as: dict[str, str] = field(default_factory=dict)


def _tree(trial: Trial) -> _Tree:
    path = trial.folder / FULL
    if not path.is_file():
        raise ResultsError(
            f"{trial.id} has no {FULL} (results saved before whole files were kept), "
            "so its files cannot be rebuilt"
        )
    tree = _Tree()
    for f in parse(path.read_text(errors="replace")):
        if f.binary or not whole(f, FULL):
            tree.unknown[f.path] = "binary" if f.binary else "only partly saved"
            continue
        if not f.lines:
            if f.status == "renamed" and f.old_path:
                tree.same_as[f.path] = f.old_path
                tree.files[f.old_path] = None
                tree.base.setdefault(f.path, None)
            continue
        old = [text for mark, text in f.lines if mark != "+"]
        new = [text for mark, text in f.lines if mark != "-"]
        if f.status == "added":
            tree.files[f.path], tree.base[f.path] = new, None
        elif f.status == "deleted":
            tree.files[f.path], tree.base[f.path] = None, old
        elif f.status == "renamed" and f.old_path:
            tree.files[f.path], tree.files[f.old_path] = new, None
            tree.base[f.old_path] = old
            tree.base.setdefault(f.path, None)
        else:
            tree.files[f.path], tree.base[f.path] = new, old
    return tree


class _Unknown(Exception):
    pass


def _base(path: str, trees: list[_Tree]) -> list[str] | None:
    for tree in trees:
        if path in tree.base:
            return tree.base[path]
    raise _Unknown(path)


def _version(tree: _Tree, path: str, trees: list[_Tree]) -> list[str] | None:
    if path in tree.files:
        return tree.files[path]
    if path in tree.same_as:
        return _base(tree.same_as[path], trees)
    return _base(path, trees)


def find_trial(folder: Path, name: str) -> Trial:
    trials = {t.id: t for t in saved_trials(folder)}
    if name not in trials:
        known = ", ".join(trials) or "none"
        raise ResultsError(f"no Trial {name!r} in {folder} (its Trials: {known})")
    return trials[name]


def trial_diff(folder: Path, first: str, second: str, only: str | None = None) -> tuple[str, list]:
    """A unified diff of the first Trial's final files against the second's, and
    notes on the files that cannot be compared.

    A file only one Trial changed is compared against the base version, taken
    from the other side of any Trial's whole-file diff of it."""
    one, two = find_trial(folder, first), find_trial(folder, second)
    a, b = _tree(one), _tree(two)
    # Every Trial's diff can give back a base version, not only these two.
    others = []
    for t in saved_trials(folder):
        if t.id not in (first, second) and (t.folder / FULL).is_file():
            others.append(_tree(t))
    trees = [a, b, *others]
    paths = sorted({p for tree in (a, b) for p in (*tree.files, *tree.unknown, *tree.same_as)})
    notes: list[str] = []
    if only is not None:
        named = _match(only, paths)
        if named is None:
            return "", [f"Neither {first} nor {second} changed {only}, so both have its base."]
        paths = [named]
    out: list[str] = []
    for path in paths:
        why = a.unknown.get(path) or b.unknown.get(path)
        if why == "binary":
            out.append(f"diff --git {first}/{path} {second}/{path}")
            out.append(f"Binary files {first}/{path} and {second}/{path} differ")
            continue
        if why:
            notes.append(f"{path} cannot be compared: a Trial's diff holds only part of it.")
            continue
        try:
            left, right = _version(a, path, trees), _version(b, path, trees)
        except _Unknown:
            changed = first if path in a.files else second
            notes.append(f"Only {changed} changed {path}, and its base version is not saved.")
            continue
        if left == right:
            continue
        out.append(f"diff --git {first}/{path} {second}/{path}")
        out += difflib.unified_diff(
            left or [],
            right or [],
            fromfile="/dev/null" if left is None else f"{first}/{path}",
            tofile="/dev/null" if right is None else f"{second}/{path}",
            lineterm="",
            n=CONTEXT,
        )
    return ("\n".join(out) + "\n" if out else ""), notes
