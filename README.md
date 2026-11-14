# build-bench

> **Warning: build-bench runs coding agents with every permission check
> bypassed.** Each Trial's agent runs commands on your machine, as you, in your
> real home folder, without asking. Trials work in their own clone with no git
> remote, but nothing else is sandboxed. Only run Experiments whose prompts,
> plans and repositories you trust.

build-bench runs the same build task under several coding-agent setups, then
measures and reviews the results so the setups can be compared. Describe an **Experiment**
in one settings file: a target repository, a base commit, an optional plan,
the **Variants** to compare (prompt, model, effort, config folder) and the
**Judges**. build-bench builds each Variant a number of times (each build is a
**Trial**), measures every Trial, has the Judges find each Trial's **Defects**, and
writes a report. The words are defined in [GLOSSARY.md](GLOSSARY.md).

Builds run with headless [Claude Code](https://code.claude.com) or the
[GitHub Copilot CLI](https://github.com/github/copilot-cli), chosen per
Variant. Judges are either one of those agents or a single model call to any
provider through LiteLLM. Nothing in build-bench assumes a language or build system.

## Install

Needs [uv](https://docs.astral.sh/uv/), git and Claude Code.

```sh
uv tool install git+https://github.com/joshystuart/build-bench
# or, from a checkout:
uv tool install .
build-bench --help
```

## Commands

Every command takes the settings file. Leave it out and the nearest `.benchrc`
is used: the one in the current folder, or else in the first folder above it
that has one, the way git finds `.git`. `judge`, `report` and `clean` use the
latest results folder for the Experiment unless you pass `--results DIR`.

| Command | What it does |
|---|---|
| `build-bench run settings.yml` | Builds every Trial into a new results folder. `--resume` (or `--results DIR`) finishes the missing and failed Trials of an existing one instead. |
| `build-bench judge settings.yml` | Measures every finished Trial, then has the Judges find its problems, merge them into Defects and check every Trial for each. Skips work already saved, so a Judge added later only judges. |
| `build-bench report settings.yml` | Writes `report.json`, `report.md` and `report.html` from the results folder alone. Starts no agent and calls no model. |
| `build-bench all settings.yml` | `run`, `judge` and `report` in series, into a new results folder. Carries on past failed Trials. |
| `build-bench clean settings.yml` | Deletes the Trial workspaces of a results folder (kept outside it, under `~/.cache/build-bench/`) and keeps everything else. |

Exit codes: `0` when everything worked, `1` when a Trial or a step of judging failed
(the rest are still saved and reported), `2` when nothing could start (bad
settings, a missing key, a Claude Code version that does not match the pin).

`run` prints one line as each Trial starts and ends:

```
[start] opus-single-1
[done]  opus-single-1 finished in 23m41s
[fail]  sonnet-subagents-2 timeout: stopped after 120 minutes
```

In a terminal, `run` also keeps a display below those lines, redrawn every
second until the last Trial ends:

```
Experiment     [██████████████░░░░░░░░░░░░░░░░]  1/4 done · 2 running · 0 failed · 31m12s elapsed
               time left: ~38m10s (from 1 finished Trial(s))
opus-single-2  [██████████░░░░░░░░░░░░░░░░░░░░]  33%  todo list 2/6
               time [██░░░░░░░░] 24m02s/120m00s   cost [███░░░░░░░] $11.90/$40.00
               now: Bash Run the tests · 12s ago
               waiting: sonnet-subagents-1 sonnet-subagents-2
```

How far a Trial has got is the best signal there is, named beside its bar:
the agent's own todo list, else the boxes ticked in the plan inside its
workspace, else its tool calls against those of finished Trials. Time left
comes from how long finished Trials took; until one finishes, only the most
the timeout allows is shown. A Trial with no news for three minutes is shown
as quiet. When the output is not a terminal (a pipe, CI) only the lines are
printed; `NO_COLOR` turns colour off.

A failed Trial is recorded with its reason and never retried on its own.
Running `run --resume` runs it again from nothing.

## The settings file

One YAML file per Experiment. Name it `.benchrc` at the root of a repository
to make it that repository's default (`repo: .`), or name it anything and pass
it to each command. Paths are relative to the settings file, except
`plan`, which is a folder inside the target repository, given relative to the
repository (it is copied to the same place in every Trial).

```yaml
name: opus-vs-sonnet            # letters, digits, . _ -
repo: ../my-project             # the target repository
base: 6ec95b3                   # any commit-ish in that repository
plan: .scratch/my-plan          # optional; may be ignored by git
results: .bench/results         # optional; this is the default
pre_build: [npm ci]             # optional; run in each Trial before the agent
checks: npm run check           # optional; run by `judge` in a copy of each Trial
concurrency: 2                  # Trials (and Judges) at once
auth: api-key                   # or: subscription; Claude Code's login, needed only if it is used
auth_env: MY_ANTHROPIC_KEY      # optional; the variable holding the login
claude_version: 2.1.319         # optional; `run` stops if another version is installed
copilot_version: 1.0.112        # optional; the same for the Copilot CLI
limits: { timeout_minutes: 120, max_budget_usd: 40 }   # per Trial, both optional
languages: [typescript]         # optional presets: rust, typescript, python, go
test_paths: ["e2e/**"]          # optional; added to the presets
ignore_paths: ["**/*.generated.ts"]
prices:                         # optional; US dollars per million tokens
  claude-opus-5-5: { input: 4.0, output: 20.0, cache_read: 0.20 }
variants:
  - name: opus-single
    agent: claude               # optional: claude (the default) or copilot
    prompt: prompts/single.md   # `{{plan}}` becomes the plan's path
    model: claude-opus-5-5
    effort: high                # optional: low, medium, high, xhigh, max
    config: configs/plain       # optional; copied fresh for each Trial
    skills:                     # optional skill folders (each holds a SKILL.md); ~ allowed
      - skills/tdd
      - ~/.agents/skills/code-review
    repeats: 3
  - name: sonnet-subagents
    prompt: prompts/subagents.md
    model: claude-sonnet-5-5
    config: configs/subagents
    repeats: 3
judges:
  - name: blind-agent
    type: claude                # an agent in a copy of the Trial: claude or copilot
    model: claude-opus-5-5
    effort: high                # optional
  - name: diff-only
    type: api                   # one model call through LiteLLM
    model: openai/gpt-5
    prompt: judges/diff.md      # optional; defaults to the shipped prompt
    api_key_env: MY_OPENAI_KEY  # optional; default is the provider's usual variable
    limits: { timeout_minutes: 10 }   # optional; falls back to the Experiment's
merge_judge: diff-only          # optional; the Judge that merges Findings (default: the first)
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
  `OPENAI_API_KEY`), or `api_key_env`. Claude Code Judges use the Experiment's
  login.
- The Copilot CLI finds its own login, so there is nothing to set: a token in
  `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN`, else the login
  `copilot login` saved in the system keychain, else the `gh` command's. An
  Experiment that uses only Copilot can leave `auth` out.

If a needed variable is missing, the command stops before the first Trial (or
the first Judge) and names it. The report says which login was used:
subscription rate limits can slow Trials down, so trust time less there.

[docs/headless-trials.md](docs/headless-trials.md) records exactly how a
Trial's agent is started and what each login does with a separate config
folder.

### Keeping Experiments in the repository they build

Commit the Experiment and let git ignore its results:

```
my-project/
  .benchrc                    # the default Experiment: repo: .
  .bench/
    prompts/  configs/  skills/   # committed
    results/                  # ignored: build-bench writes a `.gitignore` of `*` here
```

```sh
cd my-project && build-bench all
```

The repository's own `CLAUDE.md` does not reach the Trials through the results
folder, because Trial workspaces are kept elsewhere (see "What a Trial is").
Each Trial still reads the `CLAUDE.md` in its own clone, as part of what is
being built.

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
open experiments/.bench/results/calc-mul/*/report.html
```

## What a Trial is

- **Workspace:** a local clone of `repo` at `base`, with its remote removed,
  so the agent cannot push anywhere and your checkout is never touched. The
  plan folder is copied in at the same relative path. Workspaces live under
  `$XDG_CACHE_HOME/build-bench/workspaces/` (default `~/.cache`), not in the
  results folder, so nothing above them (a `CLAUDE.md`, `node_modules`, tool
  configs) comes from the repository the Experiment is kept in.
- **Config:** the Variant's `config` folder is copied into a fresh folder and
  Claude Code is pointed at it with `CLAUDE_CONFIG_DIR`, so your own
  `~/.claude` (skills, settings, memory) does not leak in. The Variant's
  `skills` are added to it as `skills/<folder name>/`, so skills that refer to
  each other as `../<skill>/` find each other; a skill with a built-in's name
  (such as `code-review`) replaces the built-in. The skills are copied into the
  results folder when a Variant first runs there, and every Trial of it,
  including ones `run --resume` builds later, gets that copy: changing the
  originals afterwards does not change the Experiment. `CLAUDE.md` files in
  folders above the workspaces *do* reach the agent; `run` warns about them.
- **Preparation:** `pre_build` steps run in the workspace first. Their time is
  not part of the Trial's time, and they use no tokens. A failing step fails
  the Trial before the agent starts.
- **Output:** everything that differs from the base commit, committed or not,
  as one diff that leaves out files git ignores (so what `pre_build` installs
  does not count). The plan folder is diffed separately.

## Copilot Variants

A Variant with `agent: copilot` is built by the GitHub Copilot CLI, which
reaches models from several providers through one subscription. Write `model`
the way Copilot names it, with a dot in the version (`claude-sonnet-5.5`).
Everything else is the same: its own clone, its own empty home folder with
the Variant's `config` and `skills`, the same diffs, measurements and Judges.

Three things differ from Claude Code, and
[docs/headless-trials.md](docs/headless-trials.md) has the detail:

- **Cost is what Copilot charged,** in its AI credits at a cent each, not list
  prices. The report says so when an Experiment has both kinds.
- **`max_budget_usd` is not enforced.** The Copilot CLI has no spending limit
  for a whole run; `timeout_minutes` still applies.
- **Skills in `~/.agents/skills/` reach every Copilot Trial,** because the CLI
  reads that folder as well as its home folder. Move them away for a clean
  comparison.

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

Boxes a build ticks in the plan are not measured: they are the build's own
claim, and a plan need not have any.

## Judges

Scores from 1 to 10 did not tell builds apart: when this tool was tried out, a
Judge gave six different builds the same five numbers. So Judges look for what
is wrong, and
`judge` works in three steps. Each skips what is already saved.

1. **Find.** Each Judge reviews each Trial and gives a Verdict: its Findings,
   each with a severity (`critical`, `major`, `minor` or `nit`), a place in the
   code and whether the Judge reproduced it, plus a note.
2. **Merge.** One Judge (`merge_judge`, else the first) groups every Finding
   worse than a nit into the Experiment's distinct Defects, saved in
   `defects.json`. The same problem found in several Trials, in different
   words, becomes one Defect, as severe as its worst Finding. A Defect keeps
   its id when later Findings join it.
3. **Check.** Each Judge is then asked, for each Trial, whether it has each
   Defect: yes, no or unsure, with the place in the code. One review misses
   much of what is there, so this is what puts every Variant on the same list.

Each Judge sees one Trial at a time under a random label (`trial-3fa9c1`),
never the Variant's name, its model or other Trials, and never changes the
saved Trial. It is shown the plan as it was before the build, never the build's
changes to the plan or its final summary: both can name the Variant, and every
build is held to the same plan. It is told the line counts and the checks
result, so it does not count them again.

Blindness has a known limit: a Trial's folder is named after its Variant, so
absolute paths that tools write into ignored files (a virtualenv's scripts,
build caches) can name it, and an Agent Judge's copy of the workspace keeps
those files.

- **Agent Judge** (`type: claude`): a headless Claude Code agent in a
  throwaway copy of the workspace with an empty config folder. The build's
  commits are folded into uncommitted changes on the base commit, since commit
  messages can name the model. It can open files, run the checks and write a
  test to reproduce a problem. When the workspace is not there (after `clean`,
  or when the results folder was copied from another machine), it gets the
  base commit with the Trial's saved diff applied instead, which leaves out
  files git ignores. That needs `repo` to hold the base commit.
- **API Judge** (`type: api`): one model call through LiteLLM with the plan,
  the diff against the base commit and the checks output. It works after
  `clean`.

Use Judges from more than one provider. A Judge tends to go easy on work from
its own model family, and the report says so when every Judge is a Claude
model like the builders. Two ways to reach other models with one account:

- **OpenRouter:** `model: openrouter/<vendor>/<model>`, with the key in
  `OPENROUTER_API_KEY`.
- **GitHub Copilot:** `model: github_copilot/<model>`. There is no key
  variable: `judge` and `all` log in before anything else starts, and the first
  time print a code to enter at github.com. LiteLLM keeps the login in
  `~/.config/litellm/github_copilot/`.

A reply that is missing a severity, or names one that does not exist, is
recorded as a failed Verdict with the reason, and `judge` runs it again next
time. A Verdict is also given again when its Judge's prompt has changed, so one
Experiment never mixes Verdicts from two prompts. The shipped prompts are in
[src/build_bench/prompts/](src/build_bench/prompts/): `judge.md` for Verdicts
(a Judge's own `prompt` replaces it), `merge.md` and `check.md`.

## The report

`report.md` has these tables, with one line per Variant unless noted:

- **Build** and **Output:** the average over finished Trials, with the lowest
  and highest.
- **Quality:** how many Trials finished and how many passed the checks, both
  out of every Trial, so a Trial that failed counts against its Variant. Then
  the Defects one Trial has, by severity, and the nits one Judge listed.
- **Defects:** one line per Defect, worst first, with the Trials of each
  Variant that have it. A Trial has a Defect when more of its Judges say it
  does than say it does not; an even split is shown as disputed.
- **Judges:** each Judge's Verdicts, Defect checks, failures, cost and time,
  kept apart from the build numbers.

Failed Trials, failed Verdicts and failed Defect checks are listed with their
reasons. The report flags Trials that ran alongside others (time comparisons
between them are weaker), an Experiment whose Trials used different Claude Code
versions, Judges that all share the builders' model family, and the login
type. `report.json` holds every number, per Trial and per Variant.
`report.html` adds a timeline per Trial on one time scale, works offline, and
has a "Show as table" alternative for every chart.

## The results folder

```
.bench/results/<experiment>/<start time>/       (or `results` from the settings)
  experiment.json  settings.yml  plan/  skills/<variant>/<skill>/
  trials/<variant>-<repeat>/
    status.json  prompt.md  stream.jsonl  stderr.log  summary.md
    transcript/main.jsonl  transcript/subagents/
    diff.patch  diff-full.patch  plan.diff  timings.json  pre_build.log
    measurements.json  checks.log
    verdicts/<judge>.json  defect-checks/<judge>.json
  defects.json
  report.json  report.md  report.html
```

The Trial workspaces sit apart, in the folder `experiment.json` names under
`workspaces` (`~/.cache/build-bench/workspaces/<experiment>-<start time>-<hash>/`),
one `<variant>-<repeat>/repo` and `config` per Trial. `clean` removes them.
Everything else every phase reads and writes is in the results folder, so a
finished Experiment can be judged again with a new Judge, or reported again,
without rebuilding (an Agent Judge without the workspaces works from the
saved diffs).

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
