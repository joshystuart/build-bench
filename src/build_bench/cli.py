"""The `build-bench` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import settings as settings_module
from .results import ResultsError, all_folders, new_folder, pick_folder, read_json

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
    judge = add("judge", "Measure each Trial, then ask each Judge for a Verdict.")
    judge.add_argument(
        "--fresh",
        action="store_true",
        help="judge the results folder again from nothing: measurements, Verdicts, "
        "Defects, Defect checks and Comparisons",
    )
    add("report", "Write the comparison of the Variants.")
    insights = add(
        "insights", "Have a model write insights.html: the results' story, around charts."
    )
    insights.add_argument(
        "--fresh",
        action="store_true",
        help="ask the model again even when the results have not changed",
    )
    diff = commands.add_parser(
        "diff",
        help="Print a unified diff of one Trial's code against another's.",
        description="Print a unified diff of one Trial's final files against another's, "
        "rebuilt from their diff-full.patch. Put the settings file first to name one.",
    )
    diff.add_argument(
        "words",
        nargs="+",
        metavar="[settings] trial trial [path]",
        help="two Trial ids, such as fast-1 slow-2, and a file to compare only that",
    )
    diff.add_argument("--results", metavar="DIR", help="results folder (default: the latest one)")
    add(
        "all",
        "run, judge, report and insights in series, into a new results folder.",
        results=False,
    )
    clean = commands.add_parser(
        "clean",
        help="Delete the Trial workspaces of a results folder, keeping the results.",
        description="Delete the Trial workspaces of a results folder, keeping the results.",
    )
    clean.add_argument(
        "settings", nargs="?", help="the Experiment's settings file (default: the nearest .benchrc)"
    )
    which = clean.add_mutually_exclusive_group()
    which.add_argument("--results", metavar="DIR", help="results folder (default: the latest one)")
    which.add_argument(
        "--all",
        action="store_true",
        help="every results folder of the Experiment, and the workspaces left behind "
        "by results folders that were deleted",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "diff":
        _diff_words(parser, args)
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


def _need_env(settings, *, judges: bool, writer: bool = False, builders: bool = True) -> None:
    """Check the logins a phase needs: the builders' (unless not `builders`), every
    Judge's with `judges`, and the insights writer's with `writer`."""
    missing = settings_module.required_env(settings, judges=judges) if builders else []
    if writer:
        missing += settings_module.writer_env(settings)
    if missing:
        raise _Unusable("missing environment variable(s): " + ", ".join(dict.fromkeys(missing)))
    logins = (list(settings.judges) if judges else []) + ([settings.writer] if writer else [])
    try:
        settings_module.log_in_judges(logins)
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
    return (
        OK
        if judge(settings, folder, fresh=getattr(args, "fresh", False), live=sys.stdout.isatty())
        else FAILED
    )


def _report(settings, args, folder: Path | None = None) -> int:
    from .report import report

    folder = folder or pick_folder(settings, args.results)
    written = report(settings, folder)
    for path in written:
        print(f"Wrote {path}")
    saved = read_json(folder / "insights.json")
    if saved is not None and folder / "insights.html" not in written:
        why = (
            "the results changed since its words were written"
            if saved.get("status") == "ok"
            else f"the last `build-bench insights` failed ({saved.get('reason')})"
        )
        print(f"insights.html was not written: {why}. `build-bench insights` asks again.")
    return OK


def _insights(settings, args, folder: Path | None = None) -> int:
    from .insights import insights

    if settings.writer is None:
        raise _Unusable("no model to write the insights: add `insights` or a Judge to the settings")
    if folder is None:
        # `all` checked every login before it built anything.
        _need_env(settings, judges=False, writer=True, builders=False)
        folder = pick_folder(settings, args.results)
        print(f"Results: {folder}")
    try:
        ok = insights(settings, folder, fresh=getattr(args, "fresh", False))
    finally:
        # The report, and the page when the words fit, whatever became of asking.
        _report(settings, args, folder)
    return OK if ok else FAILED


def _diff_words(parser: argparse.ArgumentParser, args) -> None:
    """Split `diff`'s words into the settings file, two Trials and a path. The
    first word is the settings file when there are three or more and it is a file."""
    words = list(args.words)
    args.settings = None
    if len(words) >= 3 and Path(words[0]).is_file():
        args.settings = words.pop(0)
    if not 2 <= len(words) <= 3:
        parser.error("diff takes two Trials and, optionally, one path")
    args.first, args.second = words[:2]
    args.path = words[2] if len(words) == 3 else None


def _diff(settings, args) -> int:
    from .review import trial_diff

    folder = pick_folder(settings, args.results)
    out, notes = trial_diff(folder, args.first, args.second, args.path)
    sys.stdout.write(out)
    for note in notes:
        print(note, file=sys.stderr)
    return OK


def _all(settings, args) -> int:
    # Check every login before building anything, so judging cannot fail on a
    # missing key after hours of Trials.
    _need_env(settings, judges=True, writer=settings.writer is not None)
    folder = new_folder(settings)
    done = [_run(settings, args, folder), _judge(settings, args, folder)]
    # The insights step writes the report too, and the page from the report's numbers.
    if settings.writer is None:
        _report(settings, args, folder)
    else:
        done.append(_insights(settings, args, folder))
    return OK if all(code == OK for code in done) else FAILED


def _clean(settings, args) -> int:
    import shutil

    from .clean import clean, orphans

    if not args.all:
        folder = pick_folder(settings, args.results)
        print(f"Removed {clean(folder)} workspace(s) from {folder}")
        return OK
    for folder in all_folders(settings):
        print(f"Removed {clean(folder)} workspace(s) from {folder}")
    left = orphans(settings)
    for root in left:
        shutil.rmtree(root)
    print(f"Removed {len(left)} workspace folder(s) whose results folder is gone")
    return OK


COMMANDS = {
    "run": _run,
    "judge": _judge,
    "report": _report,
    "insights": _insights,
    "diff": _diff,
    "all": _all,
    "clean": _clean,
}


if __name__ == "__main__":
    sys.exit(main())
