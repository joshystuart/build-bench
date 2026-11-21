"""`clean`: free the disk space Trial workspaces use, keeping every result."""

from __future__ import annotations

import shutil
from pathlib import Path

from .results import all_folders, cached_workspaces, owner, saved_trials, workspaces
from .settings import Settings


def clean(folder: Path) -> int:
    """Delete one results folder's workspaces. Returns how many Trials had one."""
    removed = 0
    for trial in saved_trials(folder):
        if trial.home.exists():
            shutil.rmtree(trial.home)
            removed += 1
    root = workspaces(folder)
    if root is not None and root.is_dir():
        if root.resolve().parent == cached_workspaces().resolve():
            shutil.rmtree(root)
        elif not any(root.iterdir()):
            root.rmdir()
    return removed


def orphans(settings: Settings) -> list[Path]:
    """Cached workspaces folders whose results folder no longer exists.

    A folder whose results folder cannot be told is left alone, and so is one
    an existing results folder of this Experiment still names (it was moved)."""
    cache = cached_workspaces()
    if not cache.is_dir():
        return []
    named = {root.resolve() for f in all_folders(settings) if (root := workspaces(f))}
    found = []
    for root in sorted(p for p in cache.iterdir() if p.is_dir()):
        results = owner(root, settings)
        if results is None or (results / "experiment.json").is_file():
            continue
        if root.resolve() not in named:
            found.append(root)
    return found
