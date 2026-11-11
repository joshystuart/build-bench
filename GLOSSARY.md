# build-bench

A tool that runs the same build task under several agent setups, then scores
the results so the setups can be compared.

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
Something that scores a trial: either an agent that can inspect the trial, or
a single model call that reads the evidence it is sent.
_Avoid_: Evaluator, grader, test

**Verdict**:
One judge's scores and notes for one trial.
_Avoid_: Evaluation, review, result
