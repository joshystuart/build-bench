# The report

`report` writes `report.md`, `report.html`, `report.json` and `diffs.html` from
the results folder alone. It starts no agent and calls no model. `insights`
asks a model to write up the results as one page, `insights.html` (see
[below](#the-story-insightshtml)); `report` draws that page again from the
saved words while the results stay the same.

## The tables

One line per Variant unless noted. A Trial is **built** when it finished and
left code changes. One that finished but changed nothing counts as not built:
no Judge is asked about it (they would only bill for saying it does nothing),
its checks read "– (no changes)" and do not count as passed, and it is left out
of the Output and Defect averages. Its cost and time stay in the Build
averages, because they were spent.

- **Build** and **Output:** the average over finished Trials (Build) or built
  Trials (Output), with the lowest and highest. Build has active time and wall
  time side by side (see [trials.md](trials.md#measurements)), and the most
  subagents that ran at once.
- **Quality:** how many Trials were built and how many passed the checks, both
  out of every Trial, so a Trial that failed counts against its Variant. With
  several named Checks, the Trials that passed each one (`lint 3/3 · ui 1/3`).
  Then the Defects one Trial has, by severity, and the nits one Judge listed.
- **Checks:** with several named Checks, one line per Trial with each Check's
  result: passed, failed, timed out, or – when it was not run. Results saved
  before Checks had names count as one Check named `checks`.
- **Differences:** one line per pair of Variants. See below.
- **Defects by category:** the behaviour, test-gap and scope Defects one Trial
  has, on average. Shown once any Defect has a category.
- **Defects:** one line per Defect that some Trials have and others do not,
  behaviour first, then test gaps and scope, worst first in each, with the Trials of each Variant that have it. A Trial has a
  Defect when more of its Judges say it does than say it does not; an even
  split is shown as disputed.
- **Found and checked only:** every Defect table shows, beside the Trials of a
  Variant that have a Defect, how many of them a Judge found it in with its own
  Verdict ("found") and how many have it only because a Judge answered yes when
  asked about it in a Defect check ("checked only"). A Trial where one Judge
  found it and another only checked counts as found. Checked only is weaker
  evidence: the Judge was told what to look for. Both come from the Findings
  saved on each Defect, so results judged earlier show them too.
- **Defects already in the base commit:** Defects most of the Judges' answers
  say the base commit already had (see [judging.md](judging.md#merging-and-the-base-commit)),
  with the Trials of each Variant that have them. No Variant caused them, so
  they are left out of every Variant's Defect counts and of the Differences
  table. Defect checks saved before this question was asked count as not in
  the base.
- **Defects in every Trial:** the Defects every checked Trial has. They say
  nothing about which Variant is better, and usually point at a gap in the
  plan.
- **Comparisons:** one line per pair of Variants, when a Judge has `compare`
  set: how many pairs of Trials were compared and how often each Variant's
  build was preferred. A pair counts once, for the build more of its Judges
  preferred than the other.
- **Judge agreement:** one line per Trial that two or more Judges gave a
  Verdict on: the Findings worse than a nit each Judge reported, as critical ·
  major · minor, and the spread (the most minus the fewest of each). When one
  Judge finds 2 critical problems where another finds none, trust that Trial's
  Defect counts less.
- **Judges:** each Judge's Verdicts, Defect checks, Comparisons, failures,
  cost and time, kept apart from the build numbers. Only the Judges in the
  settings file are listed and counted: what a Judge since removed left in the
  results folder is ignored. An API Judge's cost comes from LiteLLM's price
  list, or from what the provider reported with the reply (OpenRouter does)
  when the model is not on it.

Failed Trials, failed Verdicts, failed Defect checks and failed Comparisons
are listed with their reasons, and `build-bench judge` tries the failed
judging again. When merging the Findings into Defects failed, no Defect count
is known: the Defect columns and the Differences table's Defect column read
"n/a, merge failed", the Defect tables are left out, and the report says so
first. The report flags Trials that ran alongside
others (time comparisons between them are weaker; `trials_at_once` in
`report.json` counts the Trials building at one moment, not subagents),
Trials whose main agent waited idle (and whether that was at the end), an Experiment whose Trials
used different agent versions, Judges that all share the builders' model
family, checks that no Trial passed (with whether `run` saw them pass on the
base commit), regressions (a Check that passed on the base commit and failed
in a Trial; listed first), and the login type. With `check_base: mark`, a Check
that failed on the base commit reads "fails on base", in the Quality and
Checks tables, and is left out of the pass counts, since no build could pass
it. `report.json` holds every number, per Trial and
per Variant. `report.html` adds a timeline per Trial on one time scale, works
offline, and has a "Show as table" alternative for every chart.

## The story: insights.html

`report.html` is every number; `insights.html` is the few that matter, for
someone who will not read the tables. `build-bench insights` (and `all`, as
its last step) shows a model the report, each Variant's prompt and skills, the
plan, the subagents' names and a few ratios worked out for it (how much more
one Variant costs than another, how much of its time the main agent waited).
The model writes the words: a headline, up to three numbers that back it, a
heading over each chart, each Defect's title in plain words, which subagents
did what, up to four caveats, and which Variant to pick and why. It is told
that a difference "within noise" is no difference.

The page draws everything else from the report itself, so no number on it
comes from the model except its headline stats:

- **Inputs:** the model, the task, how many runs, the Judges, and a card per
  Variant.
- **Quality, cost and time:** a bar per Variant for its average, with a dot
  for each of its Trials so the spread is in plain sight: behaviour Defects
  per built Trial, cost and active minutes, the time split into the main
  agent working and waiting on subagents. The recommended Variant is
  highlighted. Beside them: Trials built, Trials that passed every Check,
  and how many differences in quality stand clear of noise.
- **Every run:** each Trial's agents on one time scale, grouped by Variant,
  each subagent coloured by the role the model gave it. A stretch with
  nothing running is cut to a short line, so the scale is active time.
- **Shared defects:** the Defects every Trial has, and those already in the
  base commit.
- **Where builds differ:** a dot per Trial for every other Defect: whether it
  has it, the Judges split, or it does not.
- **Head to head:** with Comparisons, each pair of Variants' split.
- **Caveats** and the **recommendation**.

The model is `insights` in the settings (see
[settings.md](settings.md#the-insights-writer)), else the merge Judge. Its
reply is saved in `insights.json` (and `insights.reply.md`) with a hash of
everything it was shown: `insights` asks again only when that changed or the
model did, or with `--fresh`; `report` draws the page from the saved words
only while they match the results, and otherwise says to run `insights`.
A reply that names a Variant the results do not have, or has no headline,
fails, and `insights` exits with code 1.

## The code: diffs.html

`diffs.html` shows the code each Trial produced, so you can read it and check
whether the Judges' Findings are real. It is built from the diffs every Trial
saves (`diff-full.patch`, or `diff.patch` for Trials saved before whole-file
diffs were kept), so it works after `clean` and on old results folders. Like
`report.html` it works offline, and `report.html` and `report.md` link to it.

- **Files:** one row per changed file, one column per Trial, grouped by
  Variant. Each cell is the lines added and removed. The files the fewest
  Trials touched come first, since that is where builds differ. Files the
  ignore globs match (`ignore_paths` and the presets') are folded into one row
  that opens; the page holds only the lines around their changes, not the
  whole file, because lockfiles would otherwise be a third of its size. A
  Trial that failed or changed nothing has an empty, hatched
  column. A badge counts the distinct Defects, and Findings not merged into a
  Defect, pinned in that file.
- **The diff:** pick a cell to read that Trial's diff of that file, with line
  numbers and three lines of context, or the whole file. The address records
  the Trial and the file, so a view can be linked and survives a reload.
- **Pins:** each Finding (from `verdicts/<judge>.json`) and each Defect check
  answer that says the Trial has the Defect (from `defect-checks/<judge>.json`)
  is placed by its `where`: `src/a.rs:120`, `src/a.rs:120-130`, `a.rs, line 120`
  and several of these in one string all work, and a path may be shorter or
  longer than the diff's. A pin names its Judge, severity, category, title,
  detail or evidence, whether it is a Finding or a Defect check, and the Defect
  a Finding was merged into. Line numbers are those of the final file. A pin
  with a line goes under that line, and the lines around it are shown even far
  from any change. One that names a file but no line the diff holds goes at
  the top of the file. One whose file the Trial did not change, or whose
  `where` names no file, is listed under **Not in this diff** with its words.
- **Defects:** worst first, as in the report. Open one to see every Trial
  that has it, which Judges found it in their own Verdict, which confirmed it
  in a Defect check and which said no, with a link to each pin.
- **Compare with:** with a file open, pick another Trial to see the two final
  versions diffed against each other. A Trial that did not change the file has
  the base version. Offered only between Trials with `diff-full.patch`; a file
  too large to diff in the page says to use `build-bench diff`.

`build-bench diff <trial> <trial> [path]` prints the same comparison for
every file that differs, or one, as a unified diff you can pipe into `delta`
or `less`. It rebuilds each Trial's files from its `diff-full.patch`; a file
only one Trial changed is compared against the base version, read from the
other side of a Trial's diff of it. It takes `--results` like `report`, and the
settings file goes first when you name one:
`build-bench diff bench.yml fast-1 slow-2 src/main.rs`. It stops with exit
code 2 for an unknown Trial or one saved without `diff-full.patch`.

## Is a difference real?

Two Trials of the same Variant never come out the same, so a gap between two
Variants' averages may be nothing but that spread. The Differences table
weighs each gap against it, for behaviour Defects per Trial, cost and active time.
Test-gap and scope Defects are counted in their own table and not weighed.
Defects merged before categories existed show as uncategorised and are
weighed, as they were then.

Each cell names the Variant with the lower average and by how much, then one
of three words:

- **clear:** dealing the Trials out to the two Variants at random gives a gap
  this large less than 1 time in 20.
- **possible:** less than 1 time in 5.
- **within noise:** more often than that. Do not read it as a difference.

Every way of dealing the Trials out is tried (20,000 random ones when there
are more than 100,000). With 3 Trials per Variant there are only 20 ways, so
nothing can come out clear however large the gap: it takes at least 4 Trials
each, and more to see a small difference. The Comparisons table uses the same
three words, weighing the split of preferences against a coin toss.

To see the spread directly, run the same Variant under two names before you
compare anything. Whatever separates them is noise.

## Getting a difference to show

- **Run more Trials per Variant.** Five to eight shows differences that three
  cannot.
- **Tighten the plan.** When most Defects are in every Trial, the plan left
  the same gap for every build. Say what should happen on the failure paths,
  and the Defects that remain will differ between Variants.
- **Use more than one plan.** A setup that wins on one task may not on the
  next.
- **Turn on `compare`.** See [judging.md](judging.md).

## The results folder

```
.bench/results/<experiment>/<start time>/       (or `results` from the settings)
  experiment.json  settings.yml  plan/  skills/<variant>/<skill>/
  base-checks.json  base-checks.log
  trials/<variant>-<repeat>/
    status.json  prompt.md  stream.jsonl  stderr.log  summary.md
    transcript/main.jsonl  transcript/subagents/
    diff.patch  diff-full.patch  plan.diff  timings.json  pre_build.log  post_build.log
    measurements.json  checks.log  checks/<check>.log
    verdicts/<judge>.json  defect-checks/<judge>.json
  defects.json
  comparisons/<judge>/<trial>__<trial>.json
  report.json  report.md  report.html  diffs.html
  insights.json  insights.reply.md  insights.html
```

The Trial workspaces sit apart, in the folder `experiment.json` names under
`workspaces` (`~/.cache/build-bench/workspaces/<experiment>-<start time>-<hash>/`),
one `<variant>-<repeat>/repo` and `config` per Trial, and a `results-folder`
file naming the results folder. `clean` removes them. Deleting a results folder
by hand leaves its workspaces behind; `clean --all` removes those too, along
with the workspaces of every results folder the Experiment still has. A
workspaces folder without a `results-folder` file (made before build-bench
wrote one) is matched to its results folder by name, so only this Experiment's
are found; one it cannot place is left alone.
Everything else every phase reads and writes is in the results folder, so a
finished Experiment can be judged again with a new Judge, or reported again,
without rebuilding (an Agent Judge without the workspaces works from the
saved diffs).
