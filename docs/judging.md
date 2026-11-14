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
  run the checks and write a test to reproduce a problem. When the workspace is
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

Two Judges can prefer different builds of the same pair, so read the count,
not single answers. Six or more decided Comparisons are needed before a
one-sided result can stand clear of a coin toss.

## Failures and judging again

A reply that is missing a severity, or names one that does not exist, is
recorded as a failed Verdict with the reason, and `judge` runs it again next
time. The same goes for a Defect check and a Comparison. A Verdict is also
given again when its Judge's prompt has changed, so one Experiment never mixes
Verdicts from two prompts; Defect checks and Comparisons are redone when
build-bench's own prompt for them changes. The shipped prompts are in
[src/build_bench/prompts/](../src/build_bench/prompts/): `judge.md` for
Verdicts (a Judge's own `prompt` replaces it), `merge.md`, `check.md` and
`compare.md`.

## Why LiteLLM is pinned

LiteLLM versions 1.82.7 and 1.82.8 on PyPI were malicious: they were
published on 24 March 2026 with a stolen publishing token and stole
credentials (1.82.8 ran on every Python start through a `.pth` file). PyPI
has since removed them. build-bench pins an exact later release (1.104.8) in
`pyproject.toml` and `uv.lock`, so an install never picks up a new release
unseen. Upgrade it on purpose, after reading the release.

