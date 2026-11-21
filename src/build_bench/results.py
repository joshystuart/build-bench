"""The results folder: the only thing the phases share.

<results>/<experiment>/<start time>/       (`results` defaults to .bench/results)
    experiment.json  settings.yml  plan/  skills/<variant>/<skill>/
    base-checks.json  base-checks.log
    trials/<variant>-<repeat>/
        status.json  prompt.md  stream.jsonl  stderr.log  summary.md
        transcript/main.jsonl  transcript/subagents/
        diff.patch  diff-full.patch  plan.diff  timings.json  pre_build.log
        measurements.json  checks.log  checks/<check>.log
        verdicts/<judge>.json  defect-checks/<judge>.json
    defects.json
    comparisons/<judge>/<trial>__<trial>.json
    report.json  report.md  report.html

The Trial workspaces live apart, under the cache folder, so that nothing above
them (a CLAUDE.md, node_modules, tool configs) belongs to the repository the
Experiment is kept in. experiment.json names the folder:

$XDG_CACHE_HOME/build-bench/workspaces/<experiment>-<start time>-<hash>/
    results-folder                                         (the results folder's path)
    <variant>-<repeat>/repo  <variant>-<repeat>/config     (removed by `clean`)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import Settings


class ResultsError(Exception):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


def experiment_root(settings: Settings) -> Path:
    return settings.results / settings.name


def new_folder(settings: Settings) -> Path:
    root = experiment_root(settings)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder, n = root / stamp, 1
    while folder.exists():
        n += 1
        folder = root / f"{stamp}-{n}"
    folder.mkdir(parents=True)
    # Results are kept out of git without touching the repository's .gitignore.
    # Not when the settings file is in the results folder: `*` would ignore it too.
    ignore = settings.results / ".gitignore"
    if not ignore.exists() and not settings.folder.is_relative_to(settings.results):
        ignore.write_text("*\n")
    return folder


def cached_workspaces() -> Path:
    """Where every results folder's Trial workspaces go, outside any repository."""
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "build-bench" / "workspaces"


def new_workspaces(folder: Path) -> Path:
    """A folder for a results folder's Trial workspaces."""
    return cached_workspaces() / f"{folder.parent.name}-{folder.name}-{_digest(folder)}"


def _digest(folder: Path) -> str:
    return hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:8]


#: The file in a workspaces folder that names its results folder, so `clean --all`
#: can tell when that results folder is gone.
OWNER = "results-folder"


def mark_workspaces(folder: Path) -> None:
    root = workspaces(folder)
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        (root / OWNER).write_text(f"{folder.resolve()}\n")


def owner(root: Path, settings: Settings) -> Path | None:
    """The results folder a cached workspaces folder belongs to, if that can be told.

    From its `results-folder` file; for folders made before that file, from its
    name, when the name's hash matches a results folder of this Experiment."""
    try:
        return Path((root / OWNER).read_text().strip())
    except (FileNotFoundError, NotADirectoryError):
        pass
    prefix = f"{settings.name}-"
    start, _, digest = root.name.removeprefix(prefix).rpartition("-")
    if not root.name.startswith(prefix) or not start:
        return None
    folder = experiment_root(settings) / start
    return folder if _digest(folder) == digest else None


def workspaces(folder: Path) -> Path | None:
    """The Trial workspaces' folder named in experiment.json, if any."""
    found = (read_json(folder / "experiment.json") or {}).get("workspaces")
    return Path(found) if found else None


def all_folders(settings: Settings) -> list[Path]:
    """Every results folder of the Experiment, oldest first."""
    root = experiment_root(settings)
    return sorted(
        (p for p in root.glob("*") if (p / "experiment.json").is_file()), key=_start_order
    )


def latest_folder(settings: Settings) -> Path:
    root = experiment_root(settings)
    found = all_folders(settings)
    if not found:
        raise ResultsError(f"no results folder for {settings.name!r} under {root}")
    return found[-1]


def _start_order(folder: Path) -> tuple[str, int]:
    # "20261111-090000" sorts before "20261111-090000-2" from the same second.
    parts = folder.name.split("-")
    stamp = "-".join(parts[:2])
    n = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    return stamp, n


def pick_folder(settings: Settings, given: str | None) -> Path:
    if given is None:
        return latest_folder(settings)
    folder = Path(given).resolve()
    if not (folder / "experiment.json").is_file():
        raise ResultsError(f"not a results folder: {folder}")
    return folder


@dataclass(frozen=True)
class Trial:
    folder: Path
    variant: str
    repeat: int
    #: Holds the Trial's clone (`repo`) and config folder (`config`).
    home: Path

    @property
    def id(self) -> str:
        return f"{self.variant}-{self.repeat}"

    @property
    def workspace(self) -> Path:
        return self.home / "repo"

    @property
    def status(self) -> dict | None:
        return read_json(self.folder / "status.json")

    @property
    def finished(self) -> bool:
        return (self.status or {}).get("status") == "finished"


def _trial(trial_folder: Path, variant: str, repeat: int) -> Trial:
    root = workspaces(trial_folder.parent.parent)
    # Results folders made before workspaces moved out kept them inside.
    home = root / trial_folder.name if root else trial_folder / "workspace"
    return Trial(trial_folder, variant, repeat, home)


def planned_trials(settings: Settings, folder: Path) -> list[Trial]:
    return [
        _trial(folder / "trials" / f"{v.name}-{n}", v.name, n)
        for v in settings.variants
        for n in range(1, v.repeats + 1)
    ]


def saved_trials(folder: Path) -> list[Trial]:
    """Every Trial with a status, whatever the current settings say."""
    trials = []
    for status_file in sorted((folder / "trials").glob("*/status.json")):
        status = read_json(status_file) or {}
        trials.append(
            _trial(status_file.parent, status.get("variant", "?"), int(status.get("repeat", 0)))
        )
    return trials
