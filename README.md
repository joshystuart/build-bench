# build-bench

> **Warning: build-bench runs coding agents with every permission check
> bypassed.** Each agent runs commands on your machine, as you, without asking.
> Only run Experiments whose prompts, plans and repositories you trust.

build-bench gives the same build task to several coding-agent setups, then
measures and reviews what each one built, so you can see which setup is better.

You describe an **Experiment** in one file: a repository, a plan to build, the
**Variants** to compare (a prompt, a model, a config) and the **Judges** that
review the builds. build-bench builds each Variant several times (each build is
a **Trial**), has the Judges list what is wrong with each, and writes a report.

## Install

You need [uv](https://docs.astral.sh/uv/), git, and the agent you will build
with: [Claude Code](https://code.claude.com) or the
[GitHub Copilot CLI](https://github.com/github/copilot-cli).

```sh
uv tool install git+https://github.com/joshystuart/build-bench
```

## Your first Experiment

**1. Write a plan.** Put a folder in your repository that says what to build,
for example `docs/plans/my-feature/SPEC.md`.

**2. Write the build prompt.** Save this as `.bench/prompts/build.md`:

```markdown
Build the plan in {{plan}}. Work until every acceptance criterion is met,
then run the tests.
```

**3. Write the settings.** Save this as `.benchrc` at the root of your
repository:

```yaml
name: opus-vs-sonnet
repo: .
base: main                        # the commit every build starts from
plan: docs/plans/my-feature
checks: npm test                  # how to test a build
auth: subscription                # Claude Code's login
variants:
  - name: opus
    prompt: .bench/prompts/build.md
    model: claude-opus-5-5
    repeats: 4
  - name: sonnet
    prompt: .bench/prompts/build.md
    model: claude-sonnet-5-5
    repeats: 4
judges:
  - name: gpt
    type: copilot                 # an agent that can run the code
    model: gpt-6.1-terra
  - name: sol
    type: api                     # one model call that reads the diff
    model: openrouter/openai/gpt-6.1-sol
```

**4. Set your logins.**

```sh
export CLAUDE_CODE_OAUTH_TOKEN=...   # from `claude setup-token`
export OPENROUTER_API_KEY=...        # for the `sol` Judge
copilot login                        # once, for the `gpt` Judge
```

**5. Run it.**

```sh
build-bench all
```

This builds every Trial, judges them and writes the report. Open
`.bench/results/opus-vs-sonnet/<start time>/report.html`.

## Choosing who builds

Each Variant is built by Claude Code or by the Copilot CLI.

| | Claude Code | Copilot CLI |
|---|---|---|
| In the Variant | `agent: claude` (the default) | `agent: copilot` |
| Model names | `claude-opus-5-5` | Copilot's names, with a dot: `claude-sonnet-5.5`, `gpt-6.1-terra` |
| Login | `auth: subscription` and `CLAUDE_CODE_OAUTH_TOKEN`, or `auth: api-key` and `ANTHROPIC_API_KEY` | `copilot login`; leave `auth` out if nothing uses Claude Code |

```yaml
variants:
  - name: copilot-gpt
    agent: copilot
    prompt: .bench/prompts/build.md
    model: gpt-6.1-terra
    repeats: 4
```

More in [docs/trials.md](docs/trials.md).

## Choosing who judges

A Judge is one of three types:

| Type | What it is | Good for |
|---|---|---|
| `copilot` | A Copilot CLI agent working in a copy of the build | Finding the most problems: it runs the code and writes tests |
| `claude` | A Claude Code agent working in a copy of the build | The same, when the builders are not Claude |
| `api` | One model call that reads the plan and the diff | A fast extra opinion from any provider |

**Judge with a different model family than you build with.** A model goes easy
on work from its own family.

- **Claude builds:** judge with GPT models. Use `gpt-6.1-terra` as a `copilot`
  Judge and `openrouter/openai/gpt-6.1-sol` as an `api` Judge, as in the
  example above.
- **GPT builds:** judge with Claude. Use `claude-opus-5-5` as a `claude` Judge.

Three Judges work best, with at least one of them an agent. More in
[docs/judging.md](docs/judging.md).

## Reading the report

- **Quality** shows how many Defects (distinct problems) one Trial of each
  Variant has, on average.
- **Differences** tells you whether a gap between two Variants is real or just
  the normal spread between builds: *clear*, *possible* or *within noise*.
- **Defects** lists each problem and how many Trials of each Variant have it.
- **Build** shows cost, tokens and time.

To trust a difference, use at least 4 `repeats` per Variant. With 3, nothing
can come out clear. More in [docs/report.md](docs/report.md).

## Commands

Each command takes a settings file; leave it out to use the nearest `.benchrc`.

| Command | What it does |
|---|---|
| `build-bench all` | `run`, `judge` and `report`, one after the other. |
| `build-bench run` | Builds every Trial. `--resume` finishes the missing and failed ones. |
| `build-bench judge` | Measures and judges the Trials. Skips what is already done, so you can add a Judge later. |
| `build-bench report` | Writes the report again. Calls no model. |
| `build-bench clean` | Deletes the Trial workspaces and keeps the results. |

`judge`, `report` and `clean` use the latest results unless you pass
`--results DIR`. Exit codes: `0` everything worked, `1` a Trial or a judging
step failed (the rest is still saved and reported), `2` nothing could start.

## More detail

- [docs/settings.md](docs/settings.md): every setting, logins and keys, and a
  full example.
- [docs/trials.md](docs/trials.md): what a Trial is given, the live display,
  Copilot Trials and what is measured.
- [docs/judging.md](docs/judging.md): how judging works, choosing Judges and
  comparing pairs of Trials.
- [docs/report.md](docs/report.md): every table, how differences are weighed
  and the results folder.
- [GLOSSARY.md](GLOSSARY.md): what the words mean.
- [AGENTS.md](AGENTS.md): working on build-bench itself.

## Licence

MIT. See [LICENSE](LICENSE).
