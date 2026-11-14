# Tools, Approvals, and Sandbox

AgentOS tools give the agent useful capabilities. Policy layers, approval
surfaces, workspace constraints, and sandbox posture control how those tools are
allowed to act.

Use this page before running unattended automation, file edits, shell commands,
or channel-connected agents.

For a focused permissions guide, see
[`approvals-and-permissions.md`](approvals-and-permissions.md).

## Built-In Tool Areas

| Area | Examples |
| --- | --- |
| Filesystem | `read_file`, `write_file`, `edit_file`, `list_dir`, `glob_search`, `grep_search`, spreadsheet reads. |
| Shell and code | `exec_command`, `background_process`, `process`, `execute_code`. |
| Git | `git_status`, `git_diff`, `git_log`, `git_commit`, `apply_patch`. |
| Web | `web_search`, `web_fetch`, `http_request`, `x_search` (X/Twitter via xAI — see [x-search.md](x-search.md); hidden without an xAI key). |
| Browser | `browser` — drive a real Chromium (navigate, snapshot, click, type, fill, wait, eval, dialog, screenshot) via the `agent-browser` engine. Hidden until the binary is installed. **Runs outside the sandbox** (Chromium can't run inside bubblewrap/seatbelt). See [features/browser.md](features/browser.md). |
| Memory | `memory_search`, `memory_save`, `memory_get`, `memory_delete`, `memory`. |
| Sessions | `sessions_send`, `sessions_spawn`, `sessions_list`, `sessions_history`, `session_status`, `session_rename`. `sessions_spawn` with `runtime="acp"` delegates to an external coding harness (Claude Code, Codex, ...); see [acp-agents.md](acp-agents.md). |
| ACP harnesses | `acp` — operator tool (not exposed by default) for harness sessions: `agents`, `sessions`, `status`, `cancel`, `close`, `set_mode`, `set_model`, `doctor`. Harnesses run as the gateway user, **outside the AgentOS sandbox**, with their own tools; their permission prompts follow `[acp] permission_mode`. |
| Artifacts | `publish_artifact`. The mime decides the rendering: some draw inline in chat rather than as a download chip — see [artifacts-and-media.md](artifacts-and-media.md#inline-charts). |
| Media | image generation, PDF, TTS, and media helpers. |
| Skills | `skill_list`, `skill_view`, `skill_create`, `skill_edit`, `install_skill_deps`. |
| Control | cron scheduling and gateway control operations. |
| Channels/platforms | messaging, chat, and media helpers across supported channel adapters. |
| User interaction | `ask_user` — structured questions with 2-4 options each. Presenting the question ends the turn; the answer arrives as the next user message. The Web UI renders a clickable card; the CLI and channels render a numbered list you answer by typing. Hidden on surfaces with nobody to reply (cron, subagents, heartbeats); channel DMs count as having a responder. |
| Plan mode | `/plan` flips the session into research-only planning: the turn's tool surface narrows to a read/search/analysis allowlist and the agent presents its finished plan via `exit_plan_mode` (which ends the turn, like `ask_user`). Approval is out of band — the Web UI plan card's Approve button or `/plan off` — so the model cannot talk itself out of plan mode. State is per-session gateway memory; a gateway restart clears it. |

## Permission Modes

Use stricter modes when running unattended:

```sh
agentos agent --permissions restricted -m "Inspect this repo"
```

Use broader modes only when you trust the task and workspace:

```sh
agentos agent --permissions full --workspace /path/to/project -m "Run tests and fix failures"
```

For interactive work, the Web UI approvals surface can pause sensitive tool
calls for review. For automation, choose a permission mode and workspace policy
before the run starts.

Read: [`approvals-and-permissions.md`](approvals-and-permissions.md)

## Approval Flow

Sensitive actions may pause for human approval depending on permission mode,
tool policy, channel surface, and runtime configuration.

Approvals are most important for:

- filesystem writes;
- shell commands;
- external channel or webhook delivery;
- generated artifacts that will be published;
- actions that affect another service.

Use the Web UI's Inbox or Settings › Approvals when you want durable review
outside the chat scrollback.

## How a Tool Call Is Admitted

Before a model's tool call reaches hooks, policy or an approval prompt, it is
checked once:

- **Name.** An exact name always wins. On a miss, a name the model decorated
  is resolved to the one tool it clearly means: a `functions.` or `tools.`
  prefix and a trailing `:<n>` counter are dropped, case is ignored, and `/`
  reads as `_` (`functions.Read_File:0` and `web/search` run `read_file` and
  `web_search`). Only tools visible to the session are candidates, and the
  match must be unique; two tools that differ only by case, or no match at all,
  still answer `ToolNotFound`. MCP tool names (`mcp_…`) are never matched
  loosely, only exactly (a namespace prefix or counter around the exact name is
  still dropped). The name is resolved against the tools offered to the model
  as soon as the call arrives, so scheduling, timeouts, approvals, stream
  events and the saved transcript all use the real tool name.
- **Arguments.** They are validated against the tool's schema (`type`,
  `properties`, `required`, `items`, `enum`, `additionalProperties`,
  `anyOf`/`oneOf`). Lossless mistakes are repaired: `"5"` for an integer or
  number, `5.0` for an integer, `"true"`/`"false"` for a boolean, and a
  JSON-encoded string where an array or object is expected. The repaired value
  is what the transcript keeps and what the model sees on its next request.
  Nothing is turned into `null`, and arguments the schema does not mention are
  passed through unchanged. Anything else is rejected without running the tool: the result is
  an error with `error_class: "ToolArgumentsRejected"` (status reason
  `invalid_arguments`) that names every failing argument path and the type it
  expected, marked `retry_allowed: true` because a corrected call should run.
  A rejected call counts as a failed call, so repeating it unchanged
  trips the same failure-loop guard as any other failure. A tool whose schema
  cannot be read is not validated.

## Calling a Tool Directly

`POST /api/v1/tools/invoke` runs one tool without a model turn, for scripts,
CI and the desktop: `{"tool": "web_search", "args": {...}}`. It applies the
same policy an agent's tool call gets — the `[tools]` profile, allow and deny
lists, the agent's own entry, the runtime capability surface and the dispatch
policy chain — in an unattended, sandboxed context, so a tool that needs an
approval refuses (`403` with `details.code = "APPROVAL_REQUIRED"` and the
`details.approval_id` when one was queued).

The call also goes through the tool hooks configured for agent turns (see
[Tool and request hooks](#tool-and-request-hooks-python-api)), once per
request. The policy first judges the arguments as you sent them; a call it
refuses never reaches a hook. A hook's block is then a `403` like a policy
refusal, and a rewrite or a result replacement applies. `dry_run: true` returns
the resolved policy without running the tool; the hooks' `before_tool` runs in
it too, so a hook's block shows as `allowed: false`.

It denies by default: only tools whose registration declares them read-only
run. A name floor holds on top, whatever the configuration says: shell,
terminal and code execution, file and memory writes (including the `memory`
tool, `http_request` downloads, document creation, `publish_artifact`,
`env_set`), project and session renames, `sessions_spawn`, `sessions_send`,
`subagents`, `acp`, `message`, `cron` and scheduler tools, `gateway`,
`browser`, `router_control`, paid media generation and trading tools. Those
answer `404`, exactly like a tool that does not exist, and arguments that write
a file (`output_path` and the like) are refused for every tool. Lift one tool
at a time in `agentos.toml` (config RPC writes cannot add an entry):

```toml
[tools.invoke]
allow = ["create_pdf_report"]   # tool names, never patterns
```

Full status table: [HTTP API → Direct tool calls](http-api.md#direct-tool-calls).

## Workspace Controls

Read-side restriction:

```sh
agentos agent --workspace /path/to/project --workspace-strict -m "Summarize this repo"
```

Write containment:

```sh
agentos agent \
  --workspace /path/to/project \
  --workspace-lockdown \
  --scratch-dir /path/to/project/.scratch \
  -m "Investigate and prepare a minimal patch"
```

`--workspace-lockdown` is intended for automation where writes must stay inside
the workspace or scratch directory.

For shell commands, lockdown inspects the command text for write targets —
redirections (`>`, `>>`, `2>`, `&>`, `>&`, `>|`, with or without surrounding
whitespace) and `tee` (including pipe-adjacent `|tee` and long options such as
`tee --append`). The scan reads text, not shell syntax, so it deliberately fails
closed: a command that merely *mentions* a path after `>` inside quotes — say
`grep '>/dev/null' script.sh` — is refused under lockdown even though it writes
nothing. That is defense in depth, not a security boundary: a
command can still reach outside the workspace through an interpreter
(`python -c`), a copy (`cp`, `dd of=`), an in-place edit (`sed -i`), or a path
built from a shell variable. Use the OS sandbox (`agentos sandbox on`) when you
need containment that does not depend on reading the command line.

## Sandbox Commands

```sh
agentos sandbox status
agentos sandbox on
agentos sandbox full
agentos sandbox bypass
agentos sandbox reset
```

Sandbox behavior is platform-dependent. Treat `sandbox status` and `doctor` as
the source of truth for the current machine.

## Recommended Patterns

| Task | Recommended posture |
| --- | --- |
| Read-only repo summary | `--workspace` plus `--workspace-strict` |
| Local patch with tests | `--workspace`, `--workspace-lockdown`, and a scratch dir |
| Chat with possible writes | Web UI with approvals visible |
| Channel-connected agent | Conservative permissions and explicit channel config |
| Provider/debug investigation | Diagnostics on, minimal tool permissions |

## Web Safety

AgentOS web tools use provider configuration and guardrails. Use provider
diagnostics when web search behaves unexpectedly:

```sh
agentos search status
agentos search query "test query"
agentos diagnostics on
```

Search results and fetched pages are external data. They should inform the
answer, not override tool policy or user instructions.

`http_request` refuses cloud metadata endpoints (`169.254.169.254`,
`metadata.google.internal`) on every configuration; ordinary private addresses
stay reachable, so pointing it at a local dev server still works. The one
exception is the AgentOS gateway's own listener (its port on loopback, `0.0.0.0`
or this host's addresses): the agent never gets to call the Control API from
inside a turn. `web_fetch` is stricter and blocks private addresses outright.

## Credentials

Authenticated API calls work normally: an API key in an `Authorization` or
`x-api-key` header is not refused. What outbound tools refuse is credential
*material* with no legitimate destination — a PEM private key, a
vendor-prefixed provider key (`sk-ant-…`, `ghp_…`, `AKIA…`), a connection
string with an inline password, an `/etc/passwd` line.

Prefer referencing a credential over pasting it. In a shell command use
`$NAME`; for sandboxed code, have the skill declare it:

```yaml
metadata:
  agentos:
    requires:
      env: [CAP_API_KEY]
```

The value is then read at spawn time and never enters the transcript. Command
output is masked before the model sees it, and the gateway token never reaches
a child process.

Read: [`configuration.md`](configuration.md#credentials-and-child-processes)

## Tool Compression

Large tool results may be compacted before they are shown to the model. This is
normal and protects the active context window. See
[`features/tool-compression.md`](features/tool-compression.md).

## Artifacts and Media

Tool calls can publish artifacts and generate media. See
[`artifacts-and-media.md`](artifacts-and-media.md) for user-facing artifact,
document, image, PDF, and TTS workflows.

## Troubleshooting

If a tool does not run:

1. Check permission posture:

   ```sh
   agentos sandbox status
   agentos doctor
   ```

2. Check whether the gateway or channel surface requires approval.
3. Confirm the workspace path is correct.
4. Use diagnostics for repeated failures:

   ```sh
   agentos diagnostics on
   ```

## Tool and request hooks (Python API)

Code that embeds AgentOS can hook tool dispatch and provider requests by
passing `tool_hooks=[...]` and `request_hooks=[...]` to `TurnRunner`.
Hooks are plain objects with a `name`, from `agentos.engine.hooks`:

```python
from agentos.engine.hooks import Amend, Block, ReplaceResult, Rewrite, Veto


class WorkspaceGuard:
    name = "workspace-guard"
    fail_closed = True  # a crash in this hook blocks the call instead of allowing it

    def before_tool(self, call):
        path = call.tool_call.arguments.get("path", "")
        if path.startswith("/etc"):
            return Block("writes outside the workspace are not allowed")
        if path.startswith("~/"):
            return Rewrite({**call.tool_call.arguments, "path": path[2:]})
        return None  # run as is

    def after_tool(self, call, outcome):
        text = str(getattr(outcome.result, "content", "") or "")
        if "sk-" in text:
            return ReplaceResult(text.replace("sk-", "sk-[redacted]-"))
        return None


class SpendGate:
    name = "spend-gate"

    def before_request(self, call):
        if over_budget(call.session_key):
            return Veto("The daily spend limit is reached.")
        return Amend("The user is on a phone; keep answers short.")
```

- **`before_tool`** returns `None`/`Allow`, `Block(reason)` or
  `Rewrite(arguments)`.
  - Hooks run in order, and the first block wins.
  - A blocked call is not run. The model sees an error result with the
    reason (`error_class: ToolHookBlocked`).
  - Rewritten arguments are checked against the tool's schema again, and
    the tool policy and approvals still apply to them. A hook cannot use a
    rewrite to bypass either. On an approval retry the granted
    `approval_id` is kept even if the rewrite leaves it out.
  - Each hook gets its own copy of the arguments: editing
    `call.tool_call.arguments` in place changes nothing. Return `Rewrite`
    to change them.
  - When a rewrite was applied, the result carries the arguments the call
    ran with in `ToolResult.effective_arguments`.
  - The transcript keeps the call as the model made it. The tool result tells
    the model that a hook adjusted it and which arguments, by name only: a
    trailing note on a text result, a `tool_hook_adjusted_arguments` list on
    a JSON object result. The note never repeats the new values, and the
    model's record of the call keeps its own arguments.
  - Operators see the call that actually runs: an approval prompt for a
    rewritten call (and the pending-approval result on its tool card) shows
    the rewritten command, because that is what is being approved. The tool
    card's argument list stays the model's.
  - The result itself is the output of the call as it ran, so a tool that
    echoes its arguments (a written path, the command in an approval
    denial) echoes the rewritten value to the model too. A hook that puts
    something into the arguments the model must not see redacts it in
    `after_tool` with `ReplaceResult`.
  - Checks that describe the work done (the reminder to verify code changes)
    use the rewritten arguments. Guards that decide before the call runs use
    the model's arguments: the repeated-failure and loop guards, the
    per-call timeout taken from an argument (a hook cannot extend it), and
    the concurrency key of a keyed tool.
- **`after_tool`** runs after the result is settled. `outcome.result` is the
  `ToolResult` the model and the transcript will see: `content` is a string,
  and `is_error` and `execution_status` are set. `outcome.raw_result` is what
  the tool itself returned, and `outcome.exception` is set when it raised.
  - Every hook whose `before_tool` ran gets `after_tool` once, also when the
    call was blocked, its rewrite refused, denied by policy or skipped by the
    run budget; `outcome.result` is then that error result.
  - It may return `ReplaceResult(content, is_error=None)`. The replacement
    is what the model sees and what the transcript stores, on every one of
    those paths. Hooks after it see the replaced result. It is held to the
    same size cap as a tool's own result, and when it flips `is_error` the
    execution status is reset to match.
- **`before_request`** may return `Amend(context)` or `Veto(reason)`.
  - An amendment is added after the cached prompt prefix, for that request
    only, and is not persisted.
  - A veto ends the turn with error code `request_vetoed` and outcome
    `blocked`.
- **Errors in a hook** are logged and treated as "no decision". With
  `fail_closed = True`, a failing `before_tool` blocks the call, a failing
  `after_tool` withholds the result (the model gets an error,
  `error_class: ToolHookFailed`; the tool itself may already have run), and
  a failing `before_request` vetoes the request.
- **Older hooks** whose methods return nothing keep working unchanged.
- The same tool hooks also guard `POST /api/v1/tools/invoke`
  ([Calling a Tool Directly](#calling-a-tool-directly)).

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
