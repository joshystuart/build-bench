"""`clean`: free the disk space Trial workspaces use, keeping every result."""

from __future__ import annotations

import shutil
from pathlib import Path


def clean(folder: Path) -> int:
    removed = 0
    for workspace in sorted((folder / "trials").glob("*/workspace")):
        shutil.rmtree(workspace)
        removed += 1
    return removed
