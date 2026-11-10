# Sessions and History

Sessions are durable AgentOS conversations. They let you inspect past work,
resume a conversation, export a transcript, or stop a turn that is still
running.

Use sessions when you want to:

- continue a previous chat from the CLI or Web UI;
- find the session key for an artifact, cost report, or channel thread;
- export a transcript for debugging or sharing;
- abort a long-running turn without deleting the session;
- delete old sessions after you no longer need them.

## Requirements

Session commands use the gateway RPC surface. Start or connect to the gateway
before running most session commands:

```sh
agentos gateway run
```

Or use the managed background gateway:

```sh
agentos gateway start --json
agentos gateway status
```

## List Recent Sessions

```sh
agentos sessions list
agentos sessions list --limit 20
agentos sessions list --status idle
agentos sessions list --agent main
agentos sessions list --channel telegram
agentos sessions list --since 2026-05-01
agentos sessions list --search api-refactor
```

`--search` (`-q`) matches the session name, key, subject, and model, so a
renamed session is findable by the label you gave it.

Use `--json` for scripts:

```sh
agentos sessions list --json
```

## Inspect a Session

```sh
agentos sessions show <session-key>
agentos sessions show <session-key> --json
```

The output includes the resolved session key, agent id, status, model, update
time, title, and the latest preview when available.

## Rename a Session

Sessions are auto-named. Give one a label you will recognize later:

```sh
agentos sessions rename <session-key> "api-refactor"
agentos sessions rename api-refactor "bug-46"   # target by the current name
agentos sessions rename <session-key> --clear   # back to the auto name
```

Inside a chat, `/rename <name>` renames the session you are in; `/rename` with
no argument clears the name. Names are trimmed, collapsed to a single line, and
capped at 120 characters. Once set, the name shows in `sessions list`, the chat
toolbar, and the Web UI session list, and `resume`/`show`/`--search` all accept
it in place of the key.

You can also just ask the agent — "call this session api-refactor". The
`session_rename` tool renames the session the agent is running in, and only
that one; asking it to clear the name drops back to the auto name.

## Resume a Session

```sh
agentos sessions resume <session-key>
```

This opens terminal chat on the existing session. Use it when you want to keep
the same conversation state instead of starting a fresh chat.

## Abort a Running Turn

```sh
agentos sessions abort <session-key>
agentos sessions abort <session-key> --json
```

Abort stops the running turn if one exists, together with any coding-agent or
subagent runs it spawned, and returns once they have ended. It does not delete
the session.

A turn that is streaming or between steps when you stop it is asked to stop
first. The model's stream is closed at once, no further request is sent, and
the turn ends on its own with its reply so far marked `[interrupted]`. If it
has not stopped within `task_runtime.turn_cancel_grace_seconds` (default `5`;
`0` stops at once), its task is cancelled. Stopping it a second time during
that grace cancels it at once. A turn running tools, or an ACP harness turn, is
cancelled at once, so a stop never waits on a long tool.

```toml
[task_runtime]
turn_cancel_grace_seconds = 5
```

A turn that is aborted while tools are running leaves a transcript the next
turn can replay as is. Every tool call that had not finished gets a
"Cancelled" error result, and the turn ends with a note telling the model it
was interrupted, so tools may have partly run.

Subagents of one parent session run at most two at a time by default. More
spawns are accepted and wait in spawn order (`sessions_spawn` reports them as
`queued`); children of other parents are not held back. Change the default
with `subagents.max_concurrent_children` (`0` = no per-parent limit), or per
agent with `subagents.max_concurrent_children` in its entry. The hard cap on
how many children a parent may have at all is still
`max_children_per_session`.

```toml
[subagents]
max_concurrent_children = 2
```

Child runs have a separate nested pool of `task_runtime.max_nested_concurrency`
slots across all parents (default: the value of `max_concurrency`) that only
children use. While a parent's turn is still running (for example waiting on a
child with `sessions_yield` and a `session_key`), its children take nested
slots first, so they still start when every shared slot is busy. Any other
child takes a shared slot first and a nested one when the shared slots open to
children are taken. In the shared pool children always leave
`subagents.subagent_reserved_slots` (default `2`, at most `max_concurrency - 1`)
free, so your own message starts at once even while children of finished turns
are still running. A child keeps its slot until it ends. Running turns never
exceed `max_concurrency + max_nested_concurrency`, at any nesting depth,
whatever `max_concurrent_children` is.

```toml
[task_runtime]
max_concurrency = 4
# max_nested_concurrency = 4   # default: max_concurrency
```

A chain of parents each waiting inline on its own child can fill both pools;
the extra children then wait for a slot until something ends (the inline wait
has its own timeout).

If the gateway restarts while subagent runs are in progress, they are not
resumed. Each is marked `abandoned` (reason `process_restart`), and their
parent is woken once with all of its children's results, so it does not wait
forever. A second restart does not announce the same children again.

Waking the parent is durable. Each wake is written to the session database
before it is sent, and counts as delivered only once the task runtime accepts
it. If sending fails, for example while the runtime is draining, the wake is
retried after 1 s, then 5 s, then 30 s, then every minute, for up to
30 minutes. A wake still pending when the gateway stops is sent by the next
boot. A parent is never woken twice for the same group of children, and an
aborted parent is not woken at all.

- A parent that closes its spawn group (`sessions_yield` without a
  `session_key`) while children still run records the group as `awaiting`.
  The last child's completion turns it into the pending wake. If the gateway
  stops in between, the next boot wakes the parent of every closed group whose
  children have all ended.
- A parent whose interrupted turn is being resumed by restart recovery is not
  also woken for its abandoned children; their results are still added to its
  transcript.
- A wake whose delivery has had no outcome for 10 minutes is taken over and
  retried; one still not delivered 30 minutes after it was written is given up
  (`delivery_timeout`). Only then does the session's task-group card show the
  group as failed.
- Deleting a session (or pruning stale sessions) deletes its wake records.
  Delivered, given-up and awaiting records are deleted after 7 days.

## Export a Transcript

Export Markdown:

```sh
agentos sessions export <session-key>
agentos sessions export <session-key> --output session.md
```

Export JSON:

```sh
agentos sessions export <session-key> --format json --output session.json
```

Exported transcripts are useful for bug reports, audits, or moving a task into a
document. Transcripts export messages from the gateway's active chat history
window (up to the 200-message gateway limit). Remove secrets, private local paths,
provider tokens, and private channel identifiers before sharing an export publicly.

## Delete a Session

```sh
agentos sessions delete <session-key>
agentos sessions delete <session-key> --yes
```

Deleting a session is for cleanup. Export first if you may need the transcript
later.

## Projects: Group Sessions and Share Knowledge

```sh
agentos projects create "Token research" --knowledge-file notes.md
agentos projects move <session-key> <project-id>   # 'none' detaches
agentos projects show <project-id>
agentos projects update <project-id> --knowledge "Revised shared context"
agentos projects delete <project-id>               # sessions survive, detached
```

A **project** groups chat sessions and carries a shared **knowledge** text.
Projects sit above agents: sessions of any agent can join the same project,
and the project page lists its sessions grouped per agent. Every session in
the project gets the knowledge injected into its system prompt as a
`Project Knowledge` block on every turn — edit the knowledge and the next
turn of every member session sees the new version. A project's `agent` field
is only the **default agent** that "New chat in project" starts sessions
with, not a membership boundary.

- Create a session directly inside a project from the Web UI Projects page
  ("New chat in project"), or move existing sessions in and out at any time.
- Deleting a project **never deletes its sessions** — they detach, keep their
  history, and simply stop receiving the shared knowledge.
- The agent can manage projects from prompting via the `projects_create`,
  `projects_list`, `projects_update`, and `projects_move_session` tools, and
  can search sibling transcripts with `session_search scope=project`. These
  tools are scoped to the calling session — knowledge is readable/writable
  only for the session's own project, and only the calling session itself can
  be moved — because knowledge a tool writes lands in every member session's
  system prompt. Cross-project management stays on the Web UI / CLI surface.
- Existing databases migrate automatically on gateway start: old sessions
  come up project-less (`project_id` empty) and behave exactly as before.

## ACP Harness Sessions

When the agent delegates to an external coding harness with
`sessions_spawn(runtime="acp", acp_agent="claude", ...)`, the child is a
regular session whose key looks like `agent:main:acp:1a2b3c4d`. It shows up
in `agentos sessions list`, keeps its own transcript and token/cost totals,
and its final answer is pushed back to the parent with a `Stats:` line.
`oneshot` children close after one turn; `persistent` ones accept
`sessions_send` follow-ups (`steer` cancels the running turn first).
`agentos acp sessions` lists the harness side (process, resume record),
`agentos acp cancel <key>` stops a running turn and `agentos acp close
<key>` ends the harness. See [`acp-agents.md`](acp-agents.md).

## Web UI Workflow

The Web UI uses the same session system. In the control console, the
**Sessions** list in the rail lists, searches and opens sessions (by name as
well as by key); a row's `⋯` menu (or right-click) has Open in new tab,
Rename, Pin, **Move to project** (choose a project, or **No project** to
detach the session), Reset, Export, Copy session key and Delete. The
**Sessions** page (`/control/sessions`) does the same in bulk; click a row's
name to rename it inline — Enter saves, Escape cancels, and an empty value
clears the custom name. In Chat, the header `⋯` menu has Rename, Compact,
Reset session, Export and Copy session key; the header shows the name once
set. See [Web UI](web-ui.md).

Open:

```text
http://127.0.0.1:18791/control/
```

## Troubleshooting

If commands cannot reach the gateway:

```sh
agentos gateway status
agentos doctor
```

If old context appears summarized, the session may have compacted older
history. This is normal for long sessions under context pressure. Export the
session when exact text matters.

Read next:

- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)
- [`web-ui.md`](web-ui.md)
- [`operations.md`](operations.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
