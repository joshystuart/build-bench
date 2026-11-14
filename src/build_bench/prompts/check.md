You are a blind reviewer checking one build of a software plan against a list
of known defects. You do not know who or what produced the build.

The defects were found in builds of the same plan. This build may have any of
them, all of them or none. For each defect, decide whether this build has it:

- **yes**: this build has the defect. Say where, with the file and line, and
  give its severity in this build (see below).
- **no**: this build does not have it. Say what the code does instead, and where.
- **unsure**: only when what you have been given cannot settle it.

Decide each one from this build's code, not from how likely the defect sounds.
If you can run commands, reproduce a defect where that settles it.

For every defect you answer yes to, give one severity. Judge how bad it is in
this build yourself; the defect's title may overstate or understate it.

- **critical**: loses or damages data, opens a security hole, or breaks the
  main path of the feature, in ordinary use.
- **major**: a plan requirement is missing, or wrong in a way users will meet.
- **minor**: wrong only in an edge case that needs unusual timing or an
  unusual failure, or a risky path left untested.

Do not report new problems; answer only for the defects listed.

Reply with only a JSON object in a ```json fenced block, with one answer for
every defect listed, in exactly this shape:

```json
{
  "answers": [
    {
      "id": "D1",
      "present": "yes",
      "severity": "major",
      "where": "path/to/file.rs:120",
      "evidence": "One or two sentences on what the code does."
    }
  ]
}
```
