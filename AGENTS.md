# AGENTS.md

build-bench is a Python 3.12 command-line tool managed with `uv`. The words
Experiment, Variant, Trial, Judge, Verdict, Finding and Defect mean what
[GLOSSARY.md](GLOSSARY.md) says.

## Checks

One command runs every check (lint, format, types and tests):

```sh
uv run sh -c 'ruff check . && ruff format --check . && mypy && pytest -q'
```

It must pass before a change is done. CI runs the same command.

## Layout

- `src/build_bench/cli.py` — the `build-bench` command and its exit codes.
- `settings.py` — loading and checking the settings file; presets.
- `agent.py` — starting headless Claude Code or the Copilot CLI (the one
  place that knows how; see `docs/headless-trials.md`).
- `run.py` — `run`: workspaces, `pre_build`, diffs, concurrency, resume.
- `progress.py` — the live display `run` draws in a terminal, read from the
  files the Trials write.
- `transcript.py`, `lines.py` — measurements from transcripts and diffs.
- `judge.py` — `judge`: measurements and checks, then the judging steps for
  API and Agent Judges: find (Verdicts), merge (Defects) and check.
- `compare.py` — the fourth judging step, for a Judge with `compare` set: which
  of two Trials is better (Comparisons).
- `report.py`, `html_report.py` — `report`, including how a difference between
  Variants is weighed against the spread between Trials; the HTML page's markup, styles and script
  are in `templates/` (Jinja, autoescaped).
- `presets/*.yaml`, `prices.yaml`, `prompts/*.md` — data, not code.

## Tests

Tests drive the real command (`build_bench.cli.main`) and check what it
leaves behind: files in the results folder, printed lines and exit codes.
No test calls a real model.

- `tests/fake_claude.py` stands in for `claude`; the `fake` fixture puts it
  first on the PATH and controls it (fail, hang, hit the budget, report a
  version) through a JSON file. Every call it receives is recorded.
- `tests/fake_copilot.py` stands in for `copilot` the same way, and replays
  `tests/fixtures/transcripts/copilot/`: a real Copilot CLI run with one
  subagent (Copilot's own instructions, request ids and streamed fragments
  removed).
- API Judges are tested with `litellm.completion` replaced by a stub.
- `tests/replies.py` works out what a stand-in Judge replies from the prompt
  it was sent; the fake `claude` and the stub both use it.
- `tests/fixtures/transcripts/subagents/` is a real Claude Code transcript
  with two parallel subagents (personal details removed);
  `tests/fixtures/diffs/` are real `git diff` outputs.
