# The report

`report` writes `report.md`, `report.html` and `report.json` from the results
folder alone. It starts no agent and calls no model.

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
    diff.patch  diff-full.patch  plan.diff  timings.json  pre_build.log
    measurements.json  checks.log  checks/<check>.log
    verdicts/<judge>.json  defect-checks/<judge>.json
  defects.json
  comparisons/<judge>/<trial>__<trial>.json
  report.json  report.md  report.html
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
