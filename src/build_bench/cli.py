"""The `build-bench` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import settings as settings_module
from .results import ResultsError, new_folder, pick_folder

#: Exit codes: 0 all good, 1 a Trial or Verdict failed, 2 nothing could start,
#: 130 stopped with Ctrl+C.
OK, FAILED, UNUSABLE, STOPPED = 0, 1, 2, 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build-bench",
        description="Run the same build task under several agent setups and compare them.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help: str, results: bool = True) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help, description=help)
        command.add_argument(
            "settings",
            nargs="?",
            help="the Experiment's settings file (default: the nearest .benchrc)",
        )
        if results:
            command.add_argument(
                "--results", metavar="DIR", help="results folder (default: the latest one)"
            )
        return command

    run = add("run", "Build every Trial and save what happened.", results=False)
    where = run.add_mutually_exclusive_group()
    where.add_argument("--results", metavar="DIR", help="finish the Trials in this results folder")
    where.add_argument(
        "--resume", action="store_true", help="finish the Trials in the latest results folder"
    )
    add("judge", "Measure each Trial, then ask each Judge for a Verdict.")
    add("report", "Write the comparison of the Variants.")
    add("all", "run, judge and report in series, into a new results folder.", results=False)
    add("clean", "Delete the Trial workspaces of a results folder, keeping the results.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        path = args.settings or settings_module.find_default(Path.cwd())
        settings = settings_module.load(path)
    except settings_module.SettingsError as error:
        for problem in error.problems:
            print(f"error: {problem}", file=sys.stderr)
        return UNUSABLE

    try:
        return COMMANDS[args.command](settings, args)
    except (ResultsError, _Unusable) as error:
        print(f"error: {error}", file=sys.stderr)
        return UNUSABLE
    except KeyboardInterrupt:
        # Ctrl+C is how a person stops a command: say so, without a traceback.
        print("Stopped.", file=sys.stderr)
        return STOPPED


class _Unusable(Exception):
    pass


def _need_env(settings, *, judges: bool) -> None:
    missing = settings_module.required_env(settings, judges=judges)
    if missing:
        raise _Unusable("missing environment variable(s): " + ", ".join(missing))
    if judges:
        try:
            settings_module.log_in_judges(settings)
        except Exception as error:  # noqa: BLE001 - whatever LiteLLM raises, nothing can start
            raise _Unusable(f"the GitHub Copilot login failed: {error}") from None


def _run(settings, args, folder: Path | None = None) -> int:
    from .run import RunError, run

    _need_env(settings, judges=False)
    if folder is None:
        if args.resume:
            folder = pick_folder(settings, None)
        elif args.results:
            folder = pick_folder(settings, args.results)
        else:
            folder = new_folder(settings)
    print(f"Results: {folder}")
    try:
        failed = run(settings, folder, live=sys.stdout.isatty())
    except RunError as error:
        raise _Unusable(str(error)) from None
    except KeyboardInterrupt:
        print("\n`build-bench run --resume` finishes the Trials left.", file=sys.stderr)
        raise
    return FAILED if failed else OK


def _judge(settings, args, folder: Path | None = None) -> int:
    from .judge import judge

    _need_env(settings, judges=True)
    folder = folder or pick_folder(settings, args.results)
    print(f"Results: {folder}")
    return OK if judge(settings, folder) else FAILED


def _report(settings, args, folder: Path | None = None) -> int:
    from .report import report

    folder = folder or pick_folder(settings, args.results)
    written = report(settings, folder)
    for path in written:
        print(f"Wrote {path}")
    return OK


def _all(settings, args) -> int:
    # Check every login before building anything, so judging cannot fail on a
    # missing key after hours of Trials.
    _need_env(settings, judges=True)
    folder = new_folder(settings)
    built = _run(settings, args, folder)
    judged = _judge(settings, args, folder)
    _report(settings, args, folder)
    return FAILED if built != OK or judged != OK else OK


def _clean(settings, args) -> int:
    from .clean import clean

    folder = pick_folder(settings, args.results)
    removed = clean(folder)
    print(f"Removed {removed} workspace(s) from {folder}")
    return OK


COMMANDS = {"run": _run, "judge": _judge, "report": _report, "all": _all, "clean": _clean}


if __name__ == "__main__":
    sys.exit(main())
