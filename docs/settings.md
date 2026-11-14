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
    compare: { max_pairs: 12 }  # optional; also compare pairs of Trials (off when left out)
merge_judge: diff-only          # optional; the Judge that merges Findings (default: the first)
```

A settings file with missing or wrong fields is rejected before anything
runs, with every problem named.

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
