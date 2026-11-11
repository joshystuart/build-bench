# How a pinned headless Trial behaves

This records how build-bench starts one headless Claude Code Trial with its own
config folder. Every claim below comes from a real run of Claude Code 2.1.319
on Linux on 2026-11-11, in a throwaway repository, unless it says otherwise.
`src/build_bench/agent.py` starts the agent exactly this way; keep the two in
step.

## The command

In the Trial's workspace (a clone at the base commit, with no remote), with
the environment cleaned as described under "Environment":

```sh
CLAUDE_CONFIG_DIR=<trial>/workspace/config \
CLAUDE_CODE_OAUTH_TOKEN=<token> \
claude -p \
  --output-format stream-json --verbose \
  --model claude-opus-5-5 \
  --effort high \
  --permission-mode bypassPermissions \
  --session-id <a new uuid> \
  --max-budget-usd 40 \
  < prompt.md
```

For an API key, set `ANTHROPIC_API_KEY=<key>` in place of
`CLAUDE_CODE_OAUTH_TOKEN`.

- `-p` with the prompt on standard input runs one headless turn and exits.
- `--output-format stream-json --verbose` writes one JSON event per line. The
  first is `{"type":"system","subtype":"init",...}`, which lists the model,
  permission mode, `apiKeySource`, `claude_code_version`, tools and `skills`.
  The last is `{"type":"result",...}`, which holds the final summary
  (`result`), `total_cost_usd`, `subtype` and `is_error`. build-bench saves
  this stream as `stream.jsonl` and the final summary as `summary.md`.
- `--model` and `--effort` (`low`, `medium`, `high`, `xhigh`, `max`) set the
  Variant's model and effort; the init event confirmed both.
- `--permission-mode bypassPermissions` bypasses every permission check. The
  init event reported `"permissionMode": "bypassPermissions"`.
- `--session-id` fixes the transcript's file name (see below).
- `--max-budget-usd` is the spending limit (see "Limits").

## Version

```sh
$ claude --version
2.1.319 (Claude Code)
```

build-bench reads the first word. The init event also carries
`claude_code_version`.

## Separate config folder

`CLAUDE_CONFIG_DIR` points Claude Code at a folder other than `~/.claude`.
build-bench copies the Variant's `config` folder into a fresh
`trials/<id>/workspace/config` for each Trial, or starts it empty.

With an empty folder and no login variable, the run stops at once:

```
"is_error": true, "result": "Not logged in · Please run /login"
```

So a separate config folder never sees the developer's own login. Each login
type is passed through an environment variable instead.

### Subscription login

**Works**, through `CLAUDE_CODE_OAUTH_TOKEN`. Make a long-lived token with
`claude setup-token` and export it; build-bench passes it to the agent as
`CLAUDE_CODE_OAUTH_TOKEN` (`auth: subscription`, or name your own variable
with `auth_env`).

Proof run (the token for this run was the current login's access token, read
from `~/.claude/.credentials.json`, which is where Linux keeps it; macOS keeps
it in the Keychain, which a separate config folder cannot see, so use
`claude setup-token` there):

```
$ CLAUDE_CONFIG_DIR=$PWD/../cfg-b CLAUDE_CODE_OAUTH_TOKEN=... \
    claude -p "Reply with just the word hi." --output-format stream-json --verbose \
    --model claude-haiku-5-5 --effort low --permission-mode bypassPermissions </dev/null
exit=0
init: model claude-haiku-5-5, permissionMode bypassPermissions, apiKeySource none, version 2.1.319
result: hi  total_cost_usd 0.00143222  is_error False
transcript: cfg-b/projects/-<workspace path>/cfe4e7b3-a1b6-4f7f-aded-88309b2afdd1.jsonl
```

`--bare` cannot be used with this login: its help says Anthropic auth is
"strictly ANTHROPIC_API_KEY or apiKeyHelper".

### API key

Through `ANTHROPIC_API_KEY` (`auth: api-key`, or name your own variable with
`auth_env`). build-bench passes it as `ANTHROPIC_API_KEY` and removes
`CLAUDE_CODE_OAUTH_TOKEN`, so only the chosen login reaches the agent.

**Only partly verified:** no API key was available for these runs. A run with
a separate config folder and a made-up key shows Claude Code picks the key up
from the environment and sends it:

```
$ CLAUDE_CONFIG_DIR=$PWD/../cfg-c ANTHROPIC_API_KEY=sk-ant-api03-not-a-real-key \
    claude -p hi --output-format stream-json --verbose --model claude-haiku-5-5 </dev/null
exit=1
init: apiKeySource ANTHROPIC_API_KEY
result: Invalid API key · Fix external API key  is_error True  api_error_status 401
```

A successful API-key Trial still needs to be run by someone with a key.

## The developer's own skills do not load

The developer's `~/.claude` holds synced skills such as `anthropic-skills:docx`
and `cowork-plugin-management:create-cowork-plugin`. The init event's `skills`
list, same prompt and model:

- With `~/.claude`: `deep-research`, `cowork-plugin-management:cowork-plugin-customizer`,
  `cowork-plugin-management:create-cowork-plugin`, ... `anthropic-skills:docx`,
  `anthropic-skills:pdf`, `anthropic-skills:pptx`, `anthropic-skills:xlsx`, and others.
- With a separate config folder and the subscription token: only Claude
  Code's built-in skills (`deep-research`, `design`, `dataviz`, `update-config`,
  `verify`, `debug`, `code-review`, `simplify`, `batch`, ...). No
  `anthropic-skills:*`, no `cowork-plugin-management:*`.
- With a separate config folder and an API key: the same built-in list.

Skills and settings in the target repository's own `.claude/` folder still
load: they are part of what is being built.

### Instruction files above the workspace do load

Claude Code reads `CLAUDE.md` files in every folder above its working folder.
A Trial whose workspace sat below a folder holding a `CLAUDE.md` that said
"reply with only the single word PINEAPPLE" answered `What is 2+2?` with
`PINEAPPLE`. Trial workspaces live under the results folder, beside the
settings file, so `run` warns about any `CLAUDE.md` or `CLAUDE.local.md` above
it. Keep Experiments in a folder with none above it.

## Where the transcript lands

```
$CLAUDE_CONFIG_DIR/projects/<working folder, with / and . replaced by ->/<session id>.jsonl
$CLAUDE_CONFIG_DIR/projects/<same>/<session id>/subagents/agent-<id>.jsonl
$CLAUDE_CONFIG_DIR/projects/<same>/<session id>/subagents/agent-<id>.meta.json
```

The `.meta.json` names the subagent and the Agent tool call that started it:

```json
{"agentType":"general-purpose","description":"Run calc tests",
 "toolUseId":"toolu_01WuYAMqMWSUkBhNpt7hddnE","spawnDepth":1, ...}
```

build-bench copies these to `transcript/main.jsonl` and
`transcript/subagents/` in the Trial's results.

Transcript facts the measurements rely on:

- Each line has an ISO `timestamp`.
- One model message is often written as several `assistant` lines (thinking,
  then each tool call), and every one repeats the same `message.usage`. Usage
  must be counted once per `message.id`. Counted that way over the main and
  subagent transcripts of the fixture run, tokens and cost match Claude Code's
  own `total_cost_usd` (0.00673388) exactly.
- `message.usage.cache_creation` splits cache writes into
  `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`, which are
  priced differently.
- A tool call is a `tool_use` block in an `assistant` line; its result is a
  `tool_result` block, with the same id, in a later `user` line.
- Lines of type `api-request`, `api-request-blob` and `api-request-shape`
  hold the full request bodies, system prompt included. The test fixtures
  leave them out.

The fixture `tests/fixtures/transcripts/subagents/` is that real run: one
main agent that started two subagents in parallel (their waits overlap), made
a commit and left a file uncommitted. Paths, the user name and the e-mail
address were replaced, and a search of the fixture for them, for `sk-ant` and
for the home folder finds nothing.

## Limits

### Spending limit

`--max-budget-usd` stops the agent after the model call that crosses the
limit. A real run with `--max-budget-usd 0.001`:

```
exit=1
result: subtype error_max_budget_usd, is_error true, terminal_reason budget_exhausted,
        errors ["Reached maximum budget ($0.001)"], total_cost_usd 0.00166052
```

The cost passes the limit by up to one model call. Whatever the agent had
done stays behind: here it had already written `poem.txt` in the workspace.
The transcript up to that point is saved too. build-bench records the Trial as
failed with `budget: reached the spending limit of $<limit>`.

### Time limit

Claude Code has no time limit flag. build-bench starts the agent in its own
process group and, after `timeout_minutes`, sends the group `SIGTERM`, then
`SIGKILL` ten seconds later if it is still running. The Trial is recorded as
failed with `timeout: stopped after <n> minutes`; the workspace and the
transcript up to that point stay.

## Environment

When build-bench itself runs inside a Claude Code session, the environment
carries that session's markers (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_EFFORT` and others). An agent started with
them would think it is nested in that session and could inherit its effort.
build-bench removes every variable starting with `CLAUDE`, plus
`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`, then sets
`CLAUDE_CONFIG_DIR` and the one login variable. The real home folder is kept,
so tool caches under it still work.
