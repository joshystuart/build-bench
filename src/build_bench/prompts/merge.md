Several reviewers each reviewed several builds of the same software plan. The
builds are separate attempts at the same feature, so the same problem often
shows up in more than one build, described in different words and at different
line numbers.

Group the new findings below into distinct defects.

- Two findings are the same defect when the same behaviour is wrong for the
  same reason, so that one description would let a reviewer check any build
  for it. Different wording, file names or line numbers do not make them
  different.
- Two findings are different defects when they are only near each other in the
  code, or share a symptom but have different causes.
- When in doubt, keep them apart.

Place every new finding in exactly one defect: either a defect that already
exists, named by its id, or a new one.

Give each new defect a title and a description that would let a reviewer check
any build for it: say what behaviour is wrong and where in the code to look,
without one build's line numbers.

Reply with only a JSON object in a ```json fenced block, in exactly this shape.
For a defect that already exists give its id and the findings to add. For a new
defect use the id "new":

```json
{
  "defects": [
    {"id": "D3", "findings": ["F2"]},
    {
      "id": "new",
      "title": "One line naming the problem",
      "description": "What is wrong and where to look for it in any build.",
      "findings": ["F1", "F4"]
    }
  ]
}
```
