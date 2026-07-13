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
```

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
document. Remove secrets, private local paths, provider tokens, and private
channel identifiers before sharing an export publicly.

## Delete a Session

```sh
agentos sessions delete <session-key>
agentos sessions delete <session-key> --yes
```

Deleting a session is for cleanup. Export first if you may need the transcript
later.

## Web UI Workflow

The Web UI uses the same session system. In the control console, use the chat
session selector to switch sessions, inspect status, and continue recent work.

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
