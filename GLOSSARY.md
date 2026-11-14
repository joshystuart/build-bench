# build-bench

A tool that runs the same build task under several agent setups, then measures
and reviews the results so the setups can be compared.

## Language

**Experiment**:
One comparison, defined by one settings file: a target repository, a base
commit, a plan, the variants and the judges.
_Avoid_: Job, benchmark, suite

**Variant**:
One agent setup being compared: a prompt, a model, an effort level and a
config folder.
_Avoid_: Arm, condition, configuration

**Trial**:
One build of one variant from the base commit. A variant with three repeats
has three trials.
_Avoid_: Run, attempt, session

**Judge**:
Something that reviews a trial: either an agent that can inspect the trial, or
a single model call that reads the evidence it is sent.
_Avoid_: Evaluator, grader, test

**Verdict**:
One judge's findings and note for one trial.
_Avoid_: Evaluation, review, result

**Finding**:
One problem that one judge reports in one trial, with a severity: critical,
major, minor or nit.
_Avoid_: Bug, issue, comment

**Defect**:
One distinct problem in the experiment's builds, merged from every finding
that describes it. Nits are never merged into defects.
_Avoid_: Bug, issue, finding

**Defect check**:
One judge's answers, for one trial, to whether it has each defect and, when it
does, how severe the defect is there.
_Avoid_: Confirmation, verification, vote

**Comparison**:
One judge's choice between two trials of different variants: which it would
rather ship, or that they are the same.
_Avoid_: Match, duel, ranking
