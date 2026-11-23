# Judging

Scores from 1 to 10 did not tell builds apart: when this tool was tried out, a
Judge gave six different builds the same five numbers. So Judges look for what
is wrong, and `judge` works in steps. Each step skips what is already saved.

1. **Find.** Each Judge reviews each Trial and gives a Verdict: its Findings,
   each with a severity (`critical`, `major`, `minor` or `nit`), a place in the
   code and whether the Judge reproduced it, plus a note.
2. **Merge.** One Judge (`merge_judge`, else the first) groups every Finding
   worse than a nit into the Experiment's distinct Defects, saved in
   `defects.json`. The same problem found in several Trials, in different
   words, becomes one Defect. A Defect keeps its id when later Findings join
   it.
3. **Check.** Each Judge is then asked, for each Trial, whether it has each
   Defect: yes, no or unsure, with the place in the code. For a yes it also
   says how severe the Defect is in that build. One review misses much of what
   is there, so this is what puts every Variant on the same list.
4. **Compare** (only for a Judge with `compare` set). The Judge is shown two
   Trials of different Variants and says which it would rather ship.

A Trial that finished but changed nothing is measured (its cost and time
count) but no Judge is asked about it.

## How severe a Defect is

A Defect starts as severe as its worst Finding. That lets one generous Judge
make a Defect critical, so once the Judges have checked for it, the report
uses their votes instead: the middle of the severities given by every Judge
that said a Trial has it (the worse of the two middle ones when their number
is even). `report.json` keeps both, as `severity` and `found_severity`.

## What a Judge sees

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

## The two kinds of Judge

- **Agent Judge** (`type: claude` or `type: copilot`): a headless Claude Code
  or Copilot CLI agent in a throwaway copy of the workspace with an empty
  config folder. The build's commits are folded into uncommitted changes on the
  base commit, since commit messages can name the model. It can open files,
  run the checks and write a test to reproduce a problem. `copy_setup` runs in
  the copy before the Judge starts (see
  [settings.md](settings.md#where-the-checks-run)). When the workspace is
  not there (after `clean`, or when the results folder was copied from another
  machine), it gets the base commit with the Trial's saved diff applied
  instead, which leaves out files git ignores. That needs `repo` to hold the
  base commit.
- **API Judge** (`type: api`): one model call through LiteLLM with the plan,
  the diff against the base commit and the checks output. It works after
  `clean`.

Two ways to reach many models with one account as an API Judge:

- **OpenRouter:** `model: openrouter/<vendor>/<model>`, with the key in
  `OPENROUTER_API_KEY`.
- **GitHub Copilot:** `model: github_copilot/<model>`. There is no key
  variable: `judge` and `all` log in before anything else starts, and the first
  time print a code to enter at github.com. LiteLLM keeps the login in
  `~/.config/litellm/github_copilot/`.

## Choosing Judges

Use Judges from a different model family than the builders. A Judge tends to
go easy on work from its own family, and the report says so when every Judge
is a Claude model like the builders. When Claude builds, judge with GPT models:
`gpt-6.1-terra` as a Copilot Agent Judge, and `openrouter/openai/gpt-6.1-sol`
as an API Judge. When GPT models build, judge with Claude.

The tips below come from one real Experiment (six Trials of a 600-line change,
three Judges), so treat them as a starting point and check them against your
own Judges table.

- **Use three Judges.** An odd number cannot split evenly, so no Defect ends
  up disputed by a tie.
- **Make at least one an Agent Judge on a strong coding model.** It can run the
  code and write a test, and it finds the most: in that Experiment an Agent
  Judge on `gpt-6.1-terra` raised 30 of the 43 Findings and reproduced 10 of
  them.
- **Add an API Judge as a fast extra vote.** It sees only the diff, but it is
  several times faster, and on `openrouter/openai/gpt-6.1-sol` it still raised
  12 Findings. It is not always cheaper: the whole diff is sent with every
  call, and there it cost about as much per call as the Agent Judge.
- **A Judge that finds little can still check well.** An Agent Judge on
  `gemini-4-hydrogen` raised one Finding in six Trials, yet in the check step
  it agreed with the other two on all but one answer. Keep such a Judge for its
  vote, or swap it for a better finder; do not rely on it to find.
- **Give `merge_judge` to your strongest model.** The merge is one small call,
  and a poor merge splits one problem into several Defects or joins two.
- **Expect the Judges to lower severities.** In the check step, both Judges
  voted four of the five Defects first reported as critical down to minor.
- **Look at who found each Defect.** When one Judge raises most of the
  Findings, the list of Defects is mostly that model's view of what matters.

## Comparing pairs of Trials

Counting Defects can miss a small difference between two Variants. Asking
"which of these two builds is better" picks it up more often. Give a Judge
`compare` to turn it on:

```yaml
judges:
  - name: sol
    type: api
    model: openrouter/openai/gpt-6.1-sol
    compare: { max_pairs: 12 }
```

That Judge is shown up to `max_pairs` pairs of Trials, one from each of two
Variants, as build 1 and build 2, and answers 1, 2 or same with its reason.
The pairs are spread evenly over the Variants and their Trials, and which
Variant is shown first alternates, so a Judge that favours one position
favours no Variant. A larger `max_pairs` later keeps the Comparisons already
saved and adds to them.

Each Comparison is one more Judge call. An API Judge answers in seconds and an
Agent Judge, which works in copies of both builds, in a minute or more. In the
Experiment above, with two 100 KB diffs, a Comparison cost about $0.21 on
`openrouter/openai/gpt-6.1-sol` and about $0.11 on a `gpt-6.1-terra` Agent
Judge.

Two Judges can prefer different builds of the same pair, so the report counts
each pair of Trials once, for the build more of its Judges preferred than the
other (a tie counts as same). Six or more decided pairs are needed before a one-sided
result can stand clear of a coin toss. When `max_pairs` is larger than the
number of Trials, Trials are used in more than one pair; those pairs are not
independent, so the result is then more confident than it should be.

## Categories

Every Finding has a category as well as a severity:

- **behaviour:** the shipped code does the wrong thing, or not what the plan asks.
- **test-gap:** the code is correct, but its tests would miss a regression.
- **scope:** the build and the plan disagree about what to build, including a
  plan that assumes something false about the base commit.

A Verdict with a missing or unknown category fails, like one with a bad
severity, and is asked for again next time. The merge Judge sees each
Finding's category and names each new Defect's; a Defect it names none for
takes the category most of its Findings have. Findings of different
categories are never merged into one Defect: a merge reply that tries is
rejected and asked for again. The report weighs Variants on behaviour Defects
only, so several test gaps cannot outvote one bug (see
[report.md](report.md)).

## Merging and the base commit

The merge keeps two Findings apart when in doubt, because a wrong merge cannot
be undone: Defects keep their ids. One rule joins Findings: test-gap Findings
that one added test would close are the same Defect. A Defect keeps the key of
every Finding merged into it.

In the check step, every Judge that says a Trial has a Defect also says
whether the base commit already had it: yes, no or unsure. An Agent Judge's
copy holds the base commit in its history, so it is told how to read files as
the base had them and how to stash the build to run the base; no separate
checkout is made. An API Judge answers from the diff, so "unsure" is a normal
answer there. A Defect is already in the base when more answers say yes than
no, across every Judge and Trial, leaving out "unsure"; a tie is not in the
base. The report lists such Defects apart and counts them for no Variant.

## Guidance for the Judges

To steer every Judge without rewriting a prompt, name a Markdown file in
`judge_guidance`:

```yaml
judge_guidance: guidance.md     # beside the settings file
```

```markdown
The base commit has no migrations folder yet; do not report that.
A missing index on a foreign key is major.
```

Its text is added, under the heading "Guidance for this Experiment", to the
end of every judging prompt: find, merge, check and compare, for Agent and API
Judges alike. A Judge's own `prompt` replaces only the shipped find prompt,
for that Judge alone; the guidance is added to it too, and reaches the steps
whose prompts cannot be replaced. Changing the guidance file makes the next
`judge` ask again for the Verdicts, Defect checks and Comparisons. Defects
already merged keep their ids; only Findings not yet in a Defect are merged
with the new guidance. Results judged before guidance was set are not judged
again while the guidance stays unset.

## Watching judge

`judge` first measures each finished Trial (line counts, cost, and the checks
run in a copy, one Trial at a time), then works through the steps above. Each
step prints a line as it starts, saying how much it has to do and how much is
already saved, then one line per ask as it ends:

```
[judge] measure: 4 Trial(s) to measure, 0 already measured
[judge] find: 8 Verdict(s) to ask for, 0 already saved (2 Judge(s) × 4 Trial(s), 3 at a time)
[verdict] opus-single-1 by opus
[judge] merge: 17 new Finding(s) to place among 0 Defect(s), by opus
[merged] 17 finding(s) into 9 defect(s) by opus
[judge] check: 8 of 8 Judge × Trial ask(s) about 9 Defect(s), 3 at a time
```

In a terminal, `judge` also keeps a display below those lines: every step and
how far it has got, then the current step's asks.

```
Judging                       measure ✓ · find 5/8 · merge · check · 6m40s elapsed
find                          [██████████████████░░░░░░░░░░░░]  5/8 done · 3 running · 0 failed
                              time left in this step: ~3m10s (from 5 finished ask(s))
opus-single-2 by opus         agent claude-opus-5-5   time [██░░░░░░░░] 3m02s/15m00s   cost [█░░░░░░░░░] $0.41/$4.00
                              now: Bash Run the tests · 8s ago
opus-single-2 by gpt          api openai/gpt-5   time [█░░░░░░░░░] 1m02s/10m00s
                              now: waiting for the reply
opus-single-1 by opus         done in 4m12s
```

An Agent Judge's last step and cost so far come from the stream it writes. An
API Judge says nothing until it replies, so only its time against its timeout
is shown. Ctrl+C stops judging; running `judge` again carries on from what is
saved.

## Failures and judging again

A reply that is missing a severity, or names one that does not exist, is
recorded as a failed Verdict with the reason, and `judge` runs it again next
time. The same goes for a Defect check and a Comparison. A reply with no JSON
object holding the expected key is asked for once more straight away, and
fails only if the second reply has none either; the record's cost covers both. A Verdict is also
given again when its Judge's prompt has changed, so one Experiment never mixes
Verdicts from two prompts; Defect checks and Comparisons are redone when
build-bench's own prompt for them changes. The shipped prompts are in
[src/build_bench/prompts/](../src/build_bench/prompts/): `judge.md` for
Verdicts (a Judge's own `prompt` replaces it), `merge.md`, `check.md` and
`compare.md`. Changing `judge_guidance` counts as a change to every one of
them.

When the merge fails, the next Judge in the settings tries it, then the one
after, until one merges. Each failed try is kept in `defects.json` under
`failed_tries` and counted as a failure of its own Judge. When every Judge
fails, the report reads "n/a, merge failed" for every Defect count.

An API Judge whose provider refuses it (no login, no permission, or no money
left: HTTP 401, 402 or 403, or LiteLLM's budget error) would get the same
answer every time, so after the first refusal `judge` asks it nothing more,
says so once with `[judge stopped]`, and leaves its other asks unsaved. Once
the cause is fixed, the next `judge` asks it for them.

## What runs again

`judge` keeps what it saved and redoes only what has gone out of date:

- **The checks** run again when a Check, `env`, `copy_setup`, `runtime_check`
  or `checks_in_place` has changed since they last ran, or when they could not
  run last time (a failed `copy_setup`, say). The transcript and
  line measurements are kept, and no Judge is asked again for that alone.
  Measurements saved before build-bench recorded these settings are kept while
  the settings are at their defaults and the checks command is the same.
- **Verdicts** run again when the Judge's find prompt or `judge_guidance` has
  changed, and **Defect checks** and **Comparisons** when their prompt or the
  guidance has. A failed step always runs again.
- **The merge** places only Findings not yet in a Defect; existing Defects keep
  their ids.
- Nothing else is part of any key: a results folder's plan is the copy saved
  when it started, and cannot change.

`judge --fresh` judges a results folder again from nothing: it removes the
measurements, the checks' logs, the Verdicts, the Defects, the Defect checks
and the Comparisons, then runs every step, so the Defect ids start again from
`D1`. A Trial whose workspace `clean` removed keeps its measurements, as its
checks could not run again. It is how to try new settings without building
again. `all` does not take `--fresh`, because it always starts a new results
folder.

## Why LiteLLM is pinned

LiteLLM versions 1.82.7 and 1.82.8 on PyPI were malicious: they were
published on 24 March 2026 with a stolen publishing token and stole
credentials (1.82.8 ran on every Python start through a `.pth` file). PyPI
has since removed them. build-bench pins an exact later release (1.104.8) in
`pyproject.toml` and `uv.lock`, so an install never picks up a new release
unseen. Upgrade it on purpose, after reading the release.

