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

Abort stops the running turn if one exists. It does not delete the session.

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

## Web UI Workflow

The Web UI uses the same session system. In the control console, use the chat
session selector to switch sessions, inspect status, and continue recent work.
On the Sessions page, click a row's name to rename it inline — Enter saves,
Escape cancels, and an empty value clears the custom name. In Chat, the header
`⋯` menu has **Rename session** with the same keys and **Move to project** (an
in-place picker — choose a project, or **No project** to detach the current
session), the chip shows the name once set (the key stays in its tooltip and
in **Copy session key**), and the session switcher lists and searches by name
as well as by key.

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
