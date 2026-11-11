# AGENTS.md

build-bench is a Python 3.12 command-line tool managed with `uv`. The words
Experiment, Variant, Trial, Judge and Verdict mean what
[GLOSSARY.md](GLOSSARY.md) says.

## Checks

One command runs every check (lint, format and tests):

```sh
uv run sh -c 'ruff check . && ruff format --check . && pytest -q'
```

It must pass before a change is done. CI runs the same command.

## Layout

- `src/build_bench/cli.py` — the `build-bench` command and its exit codes.
- `settings.py` — loading and checking the settings file; presets.
- `agent.py` — starting headless Claude Code (the one place that knows how;
  see `docs/headless-trials.md`).
- `run.py` — `run`: workspaces, `pre_build`, diffs, concurrency, resume.
- `transcript.py`, `lines.py` — measurements from transcripts and diffs.
- `judge.py` — `judge`: measurements, checks, API and Agent Judges, Verdicts.
- `report.py`, `html_report.py` — `report`.
- `presets/*.yaml`, `prices.yaml`, `prompts/judge.md` — data, not code.

## Tests

Tests drive the real command (`build_bench.cli.main`) and check what it
leaves behind: files in the results folder, printed lines and exit codes.
No test calls a real model.

- `tests/fake_claude.py` stands in for `claude`; the `fake` fixture puts it
  first on the PATH and controls it (fail, hang, hit the budget, report a
  version) through a JSON file. Every call it receives is recorded.
- API Judges are tested with `litellm.completion` replaced by a stub.
- `tests/fixtures/transcripts/subagents/` is a real Claude Code transcript
  with two parallel subagents (personal details removed);
  `tests/fixtures/diffs/` are real `git diff` outputs.
