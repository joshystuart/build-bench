# build-bench

> **Warning: build-bench runs coding agents with every permission check
> bypassed.** Each Trial's agent runs commands on your machine, as you, in your
> real home folder, without asking. Trials work in their own clone with no git
> remote, but nothing else is sandboxed. Only run Experiments whose prompts,
> plans and repositories you trust.

build-bench runs the same build task under several coding-agent setups, then
scores the results so the setups can be compared. Describe an **Experiment**
in one settings file: a target repository, a base commit, an optional plan,
the **Variants** to compare (prompt, model, effort, config folder) and the
**Judges**. build-bench builds each Variant a number of times (each build is a
**Trial**), measures every Trial, asks each Judge for a **Verdict**, and
writes a report. The words are defined in [GLOSSARY.md](GLOSSARY.md).

Builds run with headless [Claude Code](https://code.claude.com). Judges are
either Claude Code agents or a single model call to any provider through
LiteLLM. Nothing in build-bench assumes a language or build system.

## Install

Needs [uv](https://docs.astral.sh/uv/), git and Claude Code.

```sh
uv tool install git+https://github.com/joshystuart/build-bench
# or, from a checkout:
uv tool install .
build-bench --help
```

## Commands

Every command takes the settings file. `judge`, `report` and `clean` use the
latest results folder for the Experiment unless you pass `--results DIR`.

| Command | What it does |
|---|---|
| `build-bench run settings.yml` | Builds every Trial into a new results folder. `--resume` (or `--results DIR`) finishes the missing and failed Trials of an existing one instead. |
| `build-bench judge settings.yml` | Measures every finished Trial, then asks every Judge for a Verdict. Skips work already saved, so a Judge added later only judges. |
| `build-bench report settings.yml` | Writes `report.json`, `report.md` and `report.html` from the results folder alone. Starts no agent and calls no model. |
| `build-bench all settings.yml` | `run`, `judge` and `report` in series, into a new results folder. Carries on past failed Trials. |
| `build-bench clean settings.yml` | Deletes the Trial workspaces of a results folder and keeps everything else. |

Exit codes: `0` when everything worked, `1` when a Trial or a Verdict failed
(the rest are still saved and reported), `2` when nothing could start (bad
settings, a missing key, a Claude Code version that does not match the pin).

`run` prints one line as each Trial starts and ends:

```
[start] opus-single-1
[done]  opus-single-1 finished in 23m41s
[fail]  sonnet-subagents-2 timeout: stopped after 120 minutes
```

A failed Trial is recorded with its reason and never retried on its own.
Running `run --resume` runs it again from nothing.

## The settings file

One YAML file per Experiment. Paths are relative to the settings file, except
`plan`, which is a folder inside the target repository, given relative to the
repository (it is copied to the same place in every Trial).

```yaml
name: opus-vs-sonnet            # letters, digits, . _ -
repo: ../my-project             # the target repository
base: 6ec95b3                   # any commit-ish in that repository
plan: .scratch/my-plan          # optional; may be ignored by git
pre_build: [npm ci]             # optional; run in each Trial before the agent
checks: npm run check           # optional; run by `judge` in a copy of each Trial
concurrency: 2                  # Trials (and Judges) at once
auth: api-key                   # or: subscription
auth_env: MY_ANTHROPIC_KEY      # optional; the variable holding the login
claude_version: 2.1.319         # optional; `run` stops if another version is installed
limits: { timeout_minutes: 120, max_budget_usd: 40 }   # per Trial, both optional
languages: [typescript]         # optional presets: rust, typescript, python, go
test_paths: ["e2e/**"]          # optional; added to the presets
ignore_paths: ["**/*.generated.ts"]
prices:                         # optional; US dollars per million tokens
  claude-opus-5-5: { input: 4.0, output: 20.0, cache_read: 0.20 }
variants:
  - name: opus-single
    prompt: prompts/single.md   # `{{plan}}` becomes the plan's path
    model: claude-opus-5-5
    effort: high                # optional: low, medium, high, xhigh, max
    config: configs/plain       # optional; copied fresh for each Trial
    repeats: 3
  - name: sonnet-subagents
    prompt: prompts/subagents.md
    model: claude-sonnet-5-5
    config: configs/subagents
    repeats: 3
judges:
  - name: blind-agent
    type: claude                # a Claude Code agent in a copy of the Trial
    model: claude-opus-5-5
    effort: high                # optional
  - name: diff-only
    type: api                   # one model call through LiteLLM
    model: openai/gpt-5
    prompt: judges/diff.md      # optional; defaults to the shipped prompt
    api_key_env: MY_OPENAI_KEY  # optional; default is the provider's usual variable
    limits: { timeout_minutes: 10 }   # optional; falls back to the Experiment's
```

A settings file with missing or wrong fields is rejected before anything
runs, with every problem named.

### Logins and keys

Keys are read only from environment variables, never from the settings file,
and no results file holds one.

- `auth: subscription` reads `CLAUDE_CODE_OAUTH_TOKEN` (or `auth_env`). Make a
  long-lived token with `claude setup-token`.
- `auth: api-key` reads `ANTHROPIC_API_KEY` (or `auth_env`).
- An API Judge reads its provider's usual variable (for example
  `OPENAI_API_KEY`), or `api_key_env`. Agent Judges use the Experiment's login.

If a needed variable is missing, the command stops before the first Trial (or
the first Judge) and names it. The report says which login was used:
subscription rate limits can slow Trials down, so trust time less there.

[docs/headless-trials.md](docs/headless-trials.md) records exactly how a
Trial's agent is started and what each login does with a separate config
folder.

### Example Experiment

```
experiments/
  settings.yml
  prompts/build.md
  configs/plain/            # an empty folder: a Claude Code config with nothing in it
  configs/subagents/agents/ # e.g. agent definitions for a subagent setup
```

`settings.yml`:

```yaml
name: calc-mul
repo: ../calc
base: main
plan: .scratch/mul
checks: python3 -m pytest -q
concurrency: 2
auth: subscription
limits: { timeout_minutes: 20, max_budget_usd: 5 }
languages: [python]
variants:
  - name: haiku
    prompt: prompts/build.md
    model: claude-haiku-5-5
    effort: low
    config: configs/plain
    repeats: 2
  - name: sonnet
    prompt: prompts/build.md
    model: claude-sonnet-5-5
    effort: medium
    config: configs/plain
    repeats: 2
judges:
  - name: blind-agent
    type: claude
    model: claude-sonnet-5-5
  - name: diff-only
    type: api
    model: openai/gpt-5
```

`prompts/build.md`:

```markdown
Build the plan in {{plan}}. Work until every acceptance criterion is met,
tick each criterion in the plan as you finish it, and run the tests.
```

```sh
export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`
export OPENAI_API_KEY=...
build-bench all experiments/settings.yml
open experiments/results/calc-mul/*/report.html
```

## What a Trial is

- **Workspace:** a local clone of `repo` at `base`, with its remote removed,
  so the agent cannot push anywhere and your checkout is never touched. The
  plan folder is copied in at the same relative path.
- **Config:** the Variant's `config` folder is copied into a fresh folder and
  Claude Code is pointed at it with `CLAUDE_CONFIG_DIR`, so your own
  `~/.claude` (skills, settings, memory) does not leak in. `CLAUDE.md` files in
  folders above the results folder *do* reach the agent; `run` warns about
  them.
- **Preparation:** `pre_build` steps run in the workspace first. Their time is
  not part of the Trial's time, and they use no tokens. A failing step fails
  the Trial before the agent starts.
- **Output:** everything that differs from the base commit, committed or not,
  as one diff that leaves out files git ignores (so what `pre_build` installs
  does not count). The plan folder is diffed separately.

## Measurements

`judge` saves `measurements.json` for each finished Trial:

- **Cost and tokens**, including subagents, counting each model message once
  even though the transcript repeats its usage. Costs use the price table in
  [src/build_bench/prices.yaml](src/build_bench/prices.yaml); `prices` in the
  settings file overrides or adds models.
- **Time:** total, per subagent, and the time the main agent spent waiting on
  subagents (overlapping waits count once).
- **Tool time by kind:** shell commands grouped by their first word (after
  `cd dir &&` and `VAR=value`), other tools by name.
- **Production and test lines** added and removed. A file is test or
  production as a whole by `test_paths`; files matching `ignore_paths` are left
  out. `languages` adds globs from presets. Only the Rust preset also counts
  `#[cfg(test)]` blocks inside source files as test lines. A preset is a small
  YAML file; put your own in a `presets/` folder beside the settings file
  (`presets/ruby.yaml` makes `languages: [ruby]` work).
- **Checks:** whether `checks` passes, run in a throwaway copy of the
  workspace, with its output in `checks.log`.
- **Plan criteria ticked:** checkboxes ticked in the plan diff.

## Judges

Each Judge sees one Trial at a time under a random label (`trial-3fa9c1`),
never the Variant's name, its model or other Trials, and never changes the
saved Trial.

- **Agent Judge** (`type: claude`): a headless Claude Code agent in a
  throwaway copy of the workspace with an empty config folder. The build's
  commits are folded into uncommitted changes on the base commit, since commit
  messages can name the model. It can open files and run the checks. After
  `clean` it records a failed Verdict saying the workspace is gone.
- **API Judge** (`type: api`): one model call through LiteLLM with the plan,
  the diff against the base commit, the checks output and the Trial's final
  summary. It works after `clean`.

Every Verdict holds five scores from 1 to 10 (`correctness`,
`spec_completeness`, `code_quality`, `test_quality`, `honesty`), a list of bugs
and a note, plus the Judge's own cost and time. A reply missing a score or
with one out of range is recorded as a failed Verdict with the reason, and
`judge` runs it again next time. The shipped prompt is
[src/build_bench/prompts/judge.md](src/build_bench/prompts/judge.md): four
tasks, 5 means "mergeable after notable fixes", 8 means "mergeable with minor
nits", and an eight-section answer ending in the Verdict JSON.

## The report

`report.md` has one line per Variant for each number: the average over
finished Trials, the lowest and highest, and how many Trials finished. Failed
Trials and failed Verdicts are listed with their reasons and left out of the
averages. Judge cost and time are shown apart from the build numbers. It flags
Trials that ran alongside others (time comparisons between them are weaker), an
Experiment whose Trials used different Claude Code versions, and the login
type. `report.json` holds every number, per Trial and per Variant.
`report.html` adds a timeline per Trial on one time scale, works offline, and
has a "Show as table" alternative for every chart.

## The results folder

```
results/<experiment>/<start time>/
  experiment.json  settings.yml  plan/
  trials/<variant>-<repeat>/
    status.json  prompt.md  stream.jsonl  stderr.log  summary.md
    transcript/main.jsonl  transcript/subagents/
    diff.patch  diff-full.patch  plan.diff  timings.json  pre_build.log
    measurements.json  checks.log  verdicts/<judge>.json
    workspace/                (removed by `clean`)
  report.json  report.md  report.html
```

Every phase reads and writes only this folder, so a finished Experiment can be
judged again with a new Judge, or reported again, without rebuilding.

## Why LiteLLM is pinned

LiteLLM versions 1.82.7 and 1.82.8 on PyPI were malicious: they were
published on 24 March 2026 with a stolen publishing token and stole
credentials (1.82.8 ran on every Python start through a `.pth` file). PyPI
has since removed them. build-bench pins an exact later release (1.104.8) in
`pyproject.toml` and `uv.lock`, so an install never picks up a new release
unseen. Upgrade it on purpose, after reading the release.

## Development

See [AGENTS.md](AGENTS.md).

## Licence

MIT. See [LICENSE](LICENSE).
