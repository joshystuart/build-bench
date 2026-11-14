# Trials

A Trial is one build of one Variant from the base commit. This page covers what
a Trial is given, what `run` shows while it builds, how Copilot Trials differ
and what is measured afterwards.

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

## Watching a run

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

## Copilot Variants

A Variant with `agent: copilot` is built by the GitHub Copilot CLI, which
reaches models from several providers through one subscription. Write `model`
the way Copilot names it, with a dot in the version (`claude-sonnet-5.5`).
Everything else is the same: its own clone, its own empty home folder with
the Variant's `config` and `skills`, the same diffs, measurements and Judges.

Three things differ from Claude Code, and
[headless-trials.md](headless-trials.md) has the detail:

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
  [src/build_bench/prices.yaml](../src/build_bench/prices.yaml); `prices` in the
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
