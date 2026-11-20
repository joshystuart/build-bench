# The settings file

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
env:                            # optional; variables for every command and agent the Experiment starts
  PATH: $HOME/.local/share/mise/shims:${PATH}
runtime_check: node --version | grep -q '^v24\.'   # optional; must pass before each build and the checks
checks: npm run check           # optional; run by `judge` in a copy of each Trial; or a list of named Checks
checks_in_place: false          # optional; run the checks in each Trial's own workspace instead
copy_setup: [mise trust]        # optional; run in every throwaway copy of a Trial before it is used
check_base: true                # optional; `run` first makes sure `checks` passes on `base` (the default)
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
    compare: { max_pairs: 12 }  # optional; also compare pairs of Trials (off when left out)
merge_judge: diff-only          # optional; the Judge that merges Findings (default: the first)
```

A settings file with missing or wrong fields is rejected before anything
runs, with every problem named.

## Named Checks

`checks` can be one command, or a list of named Checks, each with its own
result:

```yaml
checks:
  - name: lint                  # letters, digits, . _ -; each name once
    command: pnpm lint
  - name: api
    command: pnpm test:api
  - name: ui
    command: pnpm test:ui
```

Every Check runs, in order, even after one fails, each with the Experiment's
`timeout_minutes`. A Trial's `measurements.json` keeps the result for the
checks as a whole (they pass only when every Check passes) and adds one per
Check: its name, command, whether it passed, exit code, whether it timed out,
and how long it took. Each Check's output is in `checks/<name>.log` beside
`checks.log`. A `checks` string is one Check named `checks`, and works as it
always has. Judges are told each Check's result; an API Judge is sent each
Check's output, cut to its end.

## The environment

Every process build-bench starts for an Experiment inherits the shell it was
started from, so a build can run on a different Node or Python than the base
commit pins. `env` sets variables for all of them: `runtime_check`,
`pre_build`, `copy_setup`, the checks, the building agents, and Agent Judges
in every judging step. Its `PATH` also decides which `claude` or `copilot` is
found and whose version is checked. build-bench's own `git` calls do not use
it.

```yaml
# Run everything on the runtimes mise pins in the repository.
env:
  PATH: $HOME/.local/share/mise/shims:${PATH}
runtime_check: node --version | grep -q '^v24\.'
```

- `$NAME` and `${NAME}` in a value are replaced from build-bench's own
  environment when the settings load. A name that is not set is a settings
  error, so a typo never becomes an empty `PATH` entry.
- For an agent, `env` is applied after build-bench clears the variables that
  would tie it to another session, and before it sets the config folder and
  the login, so `env` can never replace those (see
  [headless-trials.md](headless-trials.md#environment)).
- The settings file is copied into the results folder as written, so `env`
  must not hold secrets. Keep keys in the shell, as the logins below are.

`runtime_check` is one shell command that fails when the runtime is wrong. It
runs in the workspace before `pre_build`, for every Trial and for the checks
on the base commit. When it fails, the Trial fails before any agent starts,
with the end of what the command wrote to stderr as the reason. `judge` runs
it again before the checks, in the folder they will run in, after
`copy_setup`, because `judge` may be started from a different shell than
`run`. There a failure records the checks as not run, with the reason; the
Trial is still judged. It does not run before Judges.

## Where the checks run

By default `judge` runs `checks` in a throwaway copy of each Trial's
workspace, so the workspace itself never changes. A project whose setup
depends on the folder it is in can fail there for reasons that have nothing to
do with the build: Docker Compose names its project after the folder, so the
copy starts a second database on the port the Trial's own already holds, and
mise only trusts the folder it was told to trust.

Two settings deal with this:

- `checks_in_place: true` runs the checks in the Trial's own workspace. Before
  they start, build-bench saves every file git tracks and every untracked file
  git does not ignore. Afterwards it puts those back and removes untracked
  files the checks added. Files git ignores (build output, caches) stay as the
  checks left them. If the workspace cannot be put back, `judge` prints why,
  saves the reason in the Trial's `measurements.json`, does not judge that
  Trial, and exits with code 1. Delete that `measurements.json` to measure it
  again once the workspace is fixed.
- `copy_setup` is a list of commands run in order in every throwaway copy
  before it is used: the checks copy, and each Agent Judge's copy for the
  find, check and compare steps. They do not run in the Trial's own workspace
  or in the merge step's empty folder. Each is told the path of the Trial's
  own workspace in `BUILD_BENCH_WORKSPACE`. If one fails in the checks copy,
  the checks are recorded as not run, with the reason. If one fails in a
  Judge's copy, that judging step fails with the reason and no agent starts;
  the next `judge` tries it again. Its output is in `checks.log`, or beside
  the Judge's own output as `<judge>.copy_setup.log`.

```yaml
# mise refuses to run in a folder it has not been told to trust.
copy_setup:
  - mise trust

# Or: the checks reuse the database the Trial's own workspace started.
checks_in_place: true
```

```yaml
# Give each copy its own Compose project, named after the Trial's workspace.
copy_setup:
  - echo "COMPOSE_PROJECT_NAME=$(basename "$(dirname "$BUILD_BENCH_WORKSPACE")")-copy" > .env
```

Neither setting undoes what the checks change outside the folder: rows written
to a database the Trial started, containers, files elsewhere on the machine.

## Checks on the base commit

Checks that fail on the base commit fail in every Trial, whatever it builds,
so they cannot tell one Variant from another. When `checks` is set, `run`
therefore starts by running `pre_build` and then `checks` in a clone of the
base commit. If either fails, `run` stops before any Trial is built, shows the
end of the output and exits with code 2. The whole output is in
`base-checks.log` in the results folder. A check that passed is not repeated
by `run --resume`.

Set `check_base: false` when the checks are meant to fail on the base commit,
for example when the base already holds failing tests for the build to make
pass.

## Logins and keys

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

[headless-trials.md](headless-trials.md) records exactly how a
Trial's agent is started and what each login does with a separate config
folder.

## Keeping Experiments in the repository they build

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
folder, because Trial workspaces are kept elsewhere (see [trials.md](trials.md)).
Each Trial still reads the `CLAUDE.md` in its own clone, as part of what is
being built.

## Example Experiment

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
  - name: gpt
    type: copilot
    model: gpt-6.1-terra
  - name: sol
    type: api
    model: openrouter/openai/gpt-6.1-sol
```

`prompts/build.md`:

```markdown
Build the plan in {{plan}}. Work until every acceptance criterion is met,
tick each criterion in the plan as you finish it, and run the tests.
```

```sh
export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`
export OPENROUTER_API_KEY=...
build-bench all experiments/settings.yml
open experiments/.bench/results/calc-mul/*/report.html
```
