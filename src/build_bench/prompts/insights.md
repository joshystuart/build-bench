You are writing the words for a one-page summary of a build experiment. The
same plan was built several times by each of several agent setups, and
reviewers listed what was wrong with each build. Below are the experiment's
full results, each setup's prompt and the plan.

The page is for someone who will not read the tables. They want to know which
setup to use and why, in a minute. The page draws its own charts from the
results; you write the headline, the short headings above each chart, and the
recommendation.

## How to read the results

- A **Variant** is one setup. Call Variants by their names. In your words, say
  "build" or "run" for a **Trial**.
- The Differences table weighs every gap between two Variants against the
  spread between Trials of the same Variant. A gap marked **within noise** is
  not a difference: never say one Variant is better on that measure. **Possible**
  is a hint, not a finding. Only **clear** is a difference.
- Behaviour Defects are the quality measure. Defects in every Trial, and
  Defects already in the base commit, say nothing about which Variant is
  better; they say something about the plan or the starting code.
- When the Judges disagree a lot (the Judge agreement table), say so in a
  caveat.

## What to write

- Be honest. If nothing separates the Variants on quality, say that. If the
  cheaper or faster Variant is as good, that is the finding.
- Use only numbers that are in the results or in "Worked out for you". Round
  them as a person would ($1.83, 25%, 8 minutes).
- Keep headlines short: at most 8 words, as a statement of what the chart
  shows ("Pocock costs 25% more"), not a label ("Cost"). Wrap the one or two
  words that carry the point in single asterisks to have them stand out:
  `Same quality. Subagents added *nothing*.`
- Plain words. No jargon, no hedging, no exclamation marks.

Reply with only a JSON object in a ```json fenced block, in exactly this shape:

```json
{
  "headline": "The page's title: the one thing to take away, at most 8 words.",
  "stats": [
    {"value": "+25%", "label": "cost of pocock-subagents over simple-no-subagents"}
  ],
  "pick": "the name of the Variant you recommend, or null when none is better",
  "pick_title": "The recommendation in a few words, such as Simple prompt, no subagents",
  "task": "What the plan builds, in a few words",
  "variants": {"variant-name": "What sets this Variant apart, in at most 8 words"},
  "sections": {
    "inputs": "Heading over the setups being compared",
    "quality": "Heading over the chart of behaviour defects per build",
    "cost": "Heading over the chart of cost per build",
    "time": "Heading over the chart of active minutes per build, working and waiting on subagents",
    "runs": "Heading over every run's timeline of agents",
    "shared": "Heading over the defects every build has or the base commit had",
    "differing": "Heading over the defects some builds have and others do not",
    "comparisons": "Heading over the head-to-head comparisons, when there are any"
  },
  "defect_titles": {"D2": "The defect's title in plain words, at most 12 words"},
  "roles": [
    {"name": "Build a ticket", "subagents": ["Implement ticket 01", "Ticket 02 replace target"]}
  ],
  "caveats": [
    {"title": "4 runs per Variant.", "detail": "Differences under half a defect are noise."}
  ],
  "why": ["Short reasons for the pick, each a few words"],
  "next": ["Short next steps, each a few words"]
}
```

- `stats`: up to 3 numbers that back the headline.
- `variants`: one entry for every Variant.
- `defect_titles`: every Defect listed in the results, by id.
- `roles`: group the subagent names listed under "Subagents" into at most 5
  roles by what each did (building, reviewing, fixing, planning, merging...).
  Leave out the roles no subagent had. Use the names exactly as listed.
- `caveats`: up to 4, the things that should make a reader trust the result
  less: few runs, Judges who disagree, rate limits, failed builds.
- `why` and `next`: up to 4 each.
