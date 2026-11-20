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

For every defect you answer yes to, also say whether the base commit already
had it, before this build changed anything:

- **yes**: the base commit has the same problem; this build did not cause it.
- **no**: this build brought it in.
- **unsure**: you cannot tell from what you have been given.

If you can run commands, your working folder holds the base commit in its
history with the build's changes uncommitted on top: `git diff <base>` shows
them, and `git show <base>:path/to/file` shows a file as the base had it. To
run the base itself, `git stash --include-untracked` puts the folder back to
the base commit and `git stash pop` brings the build back.

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
      "in_base": "no",
      "where": "path/to/file.rs:120",
      "evidence": "One or two sentences on what the code does."
    }
  ]
}
```
