# Feature Catalog

AgentOS combines a personal-agent runtime with model routing, tools, memory,
channels, scheduling, and reusable skills.

## Product Surfaces

| Surface | What it is for |
| --- | --- |
| Web UI | Local control console, setup, chat sessions, approvals, logs, channels, and usage surfaces. |
| CLI chat | Interactive terminal agent work. |
| CLI agent | Single-turn automation, CI-like runs, and benchmark-style invocations. |
| Gateway RPC | Local server surface for Web UI, CLI clients, channels, and external clients. |
| Channels | Telegram, Slack, Discord, terminal, and websocket-style integrations. |

## Distinctive Features

### Pilot Router

Local routing for model tier selection. It is designed to keep easy turns cheap
and reserve expensive models for work that needs them.

Read: [`features/agentos-router.md`](features/agentos-router.md)

### Tool Compression

Large tool outputs are projected into compact provider-visible previews while
the runtime can keep richer raw results out-of-band.

Read: [`features/tool-compression.md`](features/tool-compression.md)

### Memory

Durable memory lets AgentOS recall useful user preferences, project notes,
and previous task traces without forcing every old transcript into the active
prompt.

Read: [`features/memory.md`](features/memory.md)

### Skills

Skills package task-specific guidance and scripts so the agent can load the
right operating instructions only when a task needs them.

Read: [`features/skills.md`](features/skills.md)

### Compaction and Cache Continuity

Long sessions can compact old context, preserve recent task state, and report
compaction lifecycle events.

Read: [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

### Sessions and Durable Agents

Sessions preserve conversation continuity, exports, and running-task control.
Durable agents provide named identities and defaults for recurring workstreams.

Read: [`sessions.md`](sessions.md) and [`agents.md`](agents.md)

### Usage, Diagnostics, and Permissions

Usage reports explain recent model spend. Diagnostics and replay help inspect a
turn after it runs. Permission and approval controls keep tool access matched to
the task.

Read: [`usage-and-cost.md`](usage-and-cost.md),
[`diagnostics-and-replay.md`](diagnostics-and-replay.md), and
[`approvals-and-permissions.md`](approvals-and-permissions.md)

## Core Runtime Capabilities

- Unified `TurnRunner` path across Web UI, CLI, and channels.
- Provider abstraction for OpenAI-compatible APIs, Anthropic, Ollama, and other
  configured backends.
- Streaming responses, tool calls, retries, approvals, artifacts, and final
  usage accounting.
- Durable session storage with transcript, summaries, context states, and
  replay support.
- Per-agent workspaces and durable agent entries.
- Subagent support for bounded delegation.

## Tools

AgentOS includes tools for:

- Filesystem read/write/edit/list/glob/grep.
- Shell commands, background processes, and code execution.
- Git status, diff, log, and commit.
- Web search and web fetch.
- Memory search/save/get/delete.
- Session search, session spawn/send/history/status.
- Artifact publication.
- Image generation, PDF, TTS, and media workflows.
- Spreadsheet, PPTX, DOCX, CSV, and PDF authoring through bundled skills.
- Cron and gateway administration.
- Skill listing, viewing, creating, editing, and installing dependencies.

Read: [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Skills

Bundled user-facing skills include:

- `deep-research`
- `summarize`
- `memory`
- `cron`
- `github`
- `docx`
- `pptx`
- `xlsx`
- `pdf-toolkit`
- `html-to-pdf`
- `multi-search-engine`
- `weather`
- `tmux`
- `sub-agent`

Read: [`features/skills.md`](features/skills.md)

## Scheduling

The `cron` command group manages scheduled AgentOS runs:

```sh
agentos cron list
agentos cron add \
  --every 1h \
  --text "Summarize important project updates" \
  --name hourly-project-check
agentos cron status <job-id>
agentos cron run <job-id>
agentos cron runs <job-id>
```

Scheduled jobs can deliver work through configured surfaces such as channels or
webhooks depending on the configured job.

Read: [`scheduling.md`](scheduling.md)

## Migration

AgentOS can import compatible state from OpenClaw and Hermes Agent:

```sh
agentos migrate openclaw --json
agentos migrate openclaw --apply
agentos migrate hermes --json
agentos migrate hermes --apply
```

Read: [`../MIGRATION.md`](../MIGRATION.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
