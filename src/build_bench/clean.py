"""`clean`: free the disk space Trial workspaces use, keeping every result."""

from __future__ import annotations

import shutil
from pathlib import Path

from .results import saved_trials, workspaces


def clean(folder: Path) -> int:
    removed = 0
    for trial in saved_trials(folder):
        if trial.home.exists():
            shutil.rmtree(trial.home)
            removed += 1
    root = workspaces(folder)
    if root is not None and root.is_dir() and not any(root.iterdir()):
        root.rmdir()
    return removed
