# Operations

This guide covers day-two commands: sessions, cron, cost, diagnostics, replay,
migration, durable agents, MCP, and install inventory. Use it after the first
successful chat or gateway run.

## Sessions

Sessions are durable chat/task histories. Use them to resume, inspect, export,
or clean up prior work.

```sh
agentos sessions list
agentos sessions show <session-key>
agentos sessions resume <session-key>
agentos sessions export <session-key>
agentos sessions abort <session-key>
agentos sessions delete <session-key>
```

Use session export when exact old context matters or when you want to debug a
long-running task outside the chat UI.

For resume, abort, export, and cleanup workflows, see
[`sessions.md`](sessions.md).

## Durable Agents

AgentOS supports durable agent entries, including the built-in `main`
agent.

```sh
agentos agents list
agentos agents add research --name Research --workspace /path/to/research
agentos agents delete research
```

Use durable agents when you want separate workspaces, instructions, or tool
profiles for recurring roles. Restart the gateway after agent config changes.

Keep each durable agent's instructions focused on that role instead of turning
every agent into a copy of `main`.

For agent examples and concepts, see [`agents.md`](agents.md).

## Cron and Scheduled Runs

Cron jobs run AgentOS tasks on a schedule.

Inspect jobs:

```sh
agentos cron list
agentos cron status <job-id>
agentos cron runs <job-id>
```

Add a simple recurring reminder:

```sh
agentos cron add \
  --every 1h \
  --text "Check for urgent project updates and summarize them" \
  --name hourly-project-check
```

Add a daily cron-style task:

```sh
agentos cron add \
  --cron "0 9 * * 1-5" \
  --tz "America/Los_Angeles" \
  --text "Prepare my weekday morning briefing" \
  --name weekday-briefing
```

Manage jobs:

```sh
agentos cron update <job-id> --enabled
agentos cron remove <job-id>
agentos cron run <job-id>
```

Good uses:

- morning briefings;
- recurring research digests;
- PR or CI checks;
- channel-delivered reminders;
- scheduled memory consolidation or reporting tasks.

Pair cron with channels when the output should be delivered somewhere other
than the local Web UI.

For scheduling examples, delivery options, and troubleshooting, see
[`scheduling.md`](scheduling.md).

## Cost and Usage

Inspect usage and estimated cost:

```sh
agentos cost
agentos cost --by-model
agentos cost --json
```

Use cost inspection after tool-heavy, routed, or long-context tasks to
understand actual runtime behavior.

For cost investigation workflow, see [`usage-and-cost.md`](usage-and-cost.md).

## Diagnostics

Diagnostics help explain runtime behavior without changing the core task.

```sh
agentos diagnostics status
agentos diagnostics on
agentos diagnostics off
```

Use diagnostics when investigating:

- provider retry behavior;
- router decisions;
- cache breaks;
- compaction events;
- tool-result compression;
- channel delivery failures.

Turn diagnostics off after collecting the needed evidence.

For diagnostics guidance and safe sharing notes, see
[`diagnostics-and-replay.md`](diagnostics-and-replay.md).

## Replay

Replay a recorded turn from the decision log:

```sh
agentos replay --session <session-key> --turn <turn-id>
```

Replay is useful for reproducing an agent turn, reviewing decision metadata, or
debugging behavior after the original chat has moved on.

## Migration

Preview first:

```sh
agentos migrate openclaw --json
agentos migrate hermes --json
```

Apply after reviewing the report:

```sh
agentos migrate openclaw --apply
agentos migrate hermes --apply
```

See [`../MIGRATION.md`](../MIGRATION.md) for custom paths and conflict
handling.

## MCP Server

AgentOS can run an MCP server bridge when installed with the `mcp` extra:

```sh
agentos mcp-server run
```

Install by following the [Installation](../README.md#installation) section of
the README, adding the `mcp` extra — use `use-agent-os[recommended,mcp]` in place of
`use-agent-os[recommended]`.

Use this when another MCP-capable client should access AgentOS-managed tools
or runtime surfaces.

For setup details, see [`mcp-server.md`](mcp-server.md).

## Install Inventory

Emit a reproducible workspace-state inventory:

```sh
agentos dist
```

Use this for support, release QA, or environment comparison.

## Models

Inspect available models:

```sh
agentos models list
```

In this build, model inspection can be runtime-backed. If it cannot connect,
start the gateway first:

```sh
agentos gateway run
```

For provider catalog inspection that does not require a live gateway, use:

```sh
agentos providers list
```

Read: [`providers-and-models.md`](providers-and-models.md)

## Health Checklist

For a confusing install or runtime:

```sh
agentos doctor
agentos gateway status
agentos providers list
agentos search list
agentos channels types
agentos sandbox status
```

Then turn on diagnostics only if the basic health surfaces are not enough.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
