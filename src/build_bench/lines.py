"""Measurements read from a Trial's diffs: production and test lines, ticked criteria."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out, i = out + "(?:/.*)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def matches(path: str, patterns: list[str]) -> bool:
    """Whether a repository-relative path matches any glob. `**` crosses folders."""
    return any(_glob_regex(p).match(path) for p in patterns)


@dataclass
class FileDiff:
    path: str
    binary: bool = False
    #: (mark, text) for every line shown, where mark is " ", "+" or "-".
    lines: list[tuple[str, str]] = field(default_factory=list)


def parse(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_path = new_path = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = FileDiff(path=line.split(" b/", 1)[-1])
            files.append(current)
            old_path = new_path = None
            in_hunk = False
        elif current is None:
            continue
        elif not in_hunk and line.startswith("--- "):
            old_path = line[4:].removeprefix("a/")
        elif not in_hunk and line.startswith("+++ "):
            new_path = line[4:].removeprefix("b/")
            current.path = (old_path or current.path) if new_path == "/dev/null" else new_path
        elif line.startswith("Binary files "):
            current.binary = True
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in (" ", "+", "-"):
            current.lines.append((line[0], line[1:]))
    return files


def rust_test_lines(lines: list[str]) -> set[int]:
    """Indexes of lines inside `#[cfg(test)]` items (usually `mod tests { ... }`)."""
    inside: set[int] = set()
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("#[cfg(test)]"):
            i += 1
            continue
        depth, opened, j = 0, False, i
        while j < len(lines):
            text = lines[j].split("//", 1)[0]
            depth += text.count("{") - text.count("}")
            opened = opened or "{" in text
            if (opened and depth <= 0) or (not opened and text.rstrip().endswith(";")):
                break
            j += 1
        inside.update(range(i, min(j, len(lines) - 1) + 1))
        i = j + 1
    return inside


def count_lines(
    diff: str, test_paths: list[str], ignore_paths: list[str], rust_inline: bool = False
) -> dict:
    """Added and removed lines split into production and test.

    A file is test or production as a whole by its path. With ``rust_inline``,
    lines inside `#[cfg(test)]` blocks of production `.rs` files count as test;
    this needs a diff with whole files as context.
    """
    totals = {"prod_added": 0, "prod_removed": 0, "test_added": 0, "test_removed": 0}
    files = []
    for f in parse(diff):
        if f.binary or matches(f.path, ignore_paths):
            continue
        whole_file_test = matches(f.path, test_paths)
        old = [text for mark, text in f.lines if mark != "+"]
        new = [text for mark, text in f.lines if mark != "-"]
        inline = rust_inline and f.path.endswith(".rs") and not whole_file_test
        old_tests = rust_test_lines(old) if inline else set()
        new_tests = rust_test_lines(new) if inline else set()
        counts = dict.fromkeys(totals, 0)
        old_i = new_i = 0
        for mark, _ in f.lines:
            if mark == "+":
                test = whole_file_test or new_i in new_tests
                counts["test_added" if test else "prod_added"] += 1
                new_i += 1
            elif mark == "-":
                test = whole_file_test or old_i in old_tests
                counts["test_removed" if test else "prod_removed"] += 1
                old_i += 1
            else:
                old_i += 1
                new_i += 1
        for key in totals:
            totals[key] += counts[key]
        if any(counts.values()):
            files.append({"path": f.path, **counts})
    return {**totals, "files": files}


_TICKED = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[[xX]\]")


def criteria_ticked(plan_diff: str) -> int:
    """Checkboxes ticked in the plan: ticked lines added minus ticked lines removed."""
    added = removed = 0
    for line in plan_diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+") and _TICKED.match(line[1:]):
            added += 1
        elif line.startswith("-") and _TICKED.match(line[1:]):
            removed += 1
    return added - removed
