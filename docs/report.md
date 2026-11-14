# The report

`report` writes `report.md`, `report.html` and `report.json` from the results
folder alone. It starts no agent and calls no model.

## The tables

One line per Variant unless noted.

- **Build** and **Output:** the average over finished Trials, with the lowest
  and highest.
- **Quality:** how many Trials finished and how many passed the checks, both
  out of every Trial, so a Trial that failed counts against its Variant. Then
  the Defects one Trial has, by severity, and the nits one Judge listed.
- **Differences:** one line per pair of Variants. See below.
- **Defects:** one line per Defect that some Trials have and others do not,
  worst first, with the Trials of each Variant that have it. A Trial has a
  Defect when more of its Judges say it does than say it does not; an even
  split is shown as disputed.
- **Defects in every Trial:** the Defects every checked Trial has. They say
  nothing about which Variant is better, and usually point at a gap in the
  plan.
- **Comparisons:** one line per pair of Variants, when a Judge has `compare`
  set: how many pairs of Trials were compared and how often each Variant's
  build was preferred.
- **Judges:** each Judge's Verdicts, Defect checks, Comparisons, failures,
  cost and time, kept apart from the build numbers. Only the Judges in the
  settings file are listed and counted: what a Judge since removed left in the
  results folder is ignored. An API Judge's cost comes from LiteLLM's price
  list, or from what the provider reported with the reply (OpenRouter does)
  when the model is not on it.

Failed Trials, failed Verdicts, failed Defect checks and failed Comparisons
are listed with their reasons. The report flags Trials that ran alongside
others (time comparisons between them are weaker), an Experiment whose Trials
used different agent versions, Judges that all share the builders' model
family, and the login type. `report.json` holds every number, per Trial and
per Variant. `report.html` adds a timeline per Trial on one time scale, works
offline, and has a "Show as table" alternative for every chart.

## Is a difference real?

Two Trials of the same Variant never come out the same, so a gap between two
Variants' averages may be nothing but that spread. The Differences table
weighs each gap against it, for Defects per Trial, cost and time.

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
  trials/<variant>-<repeat>/
    status.json  prompt.md  stream.jsonl  stderr.log  summary.md
    transcript/main.jsonl  transcript/subagents/
    diff.patch  diff-full.patch  plan.diff  timings.json  pre_build.log
    measurements.json  checks.log
    verdicts/<judge>.json  defect-checks/<judge>.json
  defects.json
  comparisons/<judge>/<trial>__<trial>.json
  report.json  report.md  report.html
```

The Trial workspaces sit apart, in the folder `experiment.json` names under
`workspaces` (`~/.cache/build-bench/workspaces/<experiment>-<start time>-<hash>/`),
one `<variant>-<repeat>/repo` and `config` per Trial. `clean` removes them.
Everything else every phase reads and writes is in the results folder, so a
finished Experiment can be judged again with a new Judge, or reported again,
without rebuilding (an Agent Judge without the workspaces works from the
saved diffs).
