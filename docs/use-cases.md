# Use Cases and Recipes

Use this page when you know what you want AgentOS to do, but you are not
sure which feature guide to read first.

## First Successful Run

Goal: install AgentOS, configure one provider, and send a real message.

```sh
agentos onboard
agentos gateway run
```

Then open:

```text
http://127.0.0.1:18791/control/
```

If you prefer the terminal:

```sh
agentos chat
```

Read next:

- [`quickstart.md`](quickstart.md)
- [`web-ui.md`](web-ui.md)
- [`providers-and-models.md`](providers-and-models.md)

## Reduce Model Cost

Goal: keep simple work on cheaper models and reserve stronger models for hard
turns.

```sh
agentos configure router --router recommended
agentos cost --by-model
```

Use diagnostics when you want to inspect routing and runtime behavior:

```sh
agentos diagnostics on
```

Read next:

- [`features/agentos-router.md`](features/agentos-router.md)
- [`features/tool-compression.md`](features/tool-compression.md)
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

## Work With Large Tool Results

Goal: let the agent inspect logs, pages, tables, search results, or diffs
without flooding the model context.

Start with a bounded workspace run:

```sh
agentos agent \
  --workspace /path/to/project \
  --workspace-strict \
  -m "Inspect the latest logs and summarize the actionable failures"
```

If the turn seems too slow or expensive:

```sh
agentos cost
agentos diagnostics on
```

Read next:

- [`features/tool-compression.md`](features/tool-compression.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`troubleshooting.md`](troubleshooting.md)

## Build a Repeatable Workflow

Goal: turn recurring work into reusable skills.

Find an existing skill:

```sh
agentos skills search report
agentos skills view <skill-name>
```

Read next:

- [`features/skills.md`](features/skills.md)
- [`artifacts-and-media.md`](artifacts-and-media.md)

## Remember Useful Context

Goal: preserve preferences, project notes, or reusable task context so future
turns can find them.

```sh
agentos memory status
agentos memory search "project preference"
agentos memory list
```

Inspect a stored memory file:

```sh
agentos memory show <path>
```

Read next:

- [`features/memory.md`](features/memory.md)
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

## Connect a Messaging Channel

Goal: use AgentOS from a supported messaging surface while keeping the
gateway as the local control point.

```sh
agentos channels types
agentos channels describe telegram
agentos channels add telegram --name personal
agentos gateway restart
agentos channels status personal --json
```

Read next:

- [`channels.md`](channels.md)
- [`configuration.md`](configuration.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Schedule Recurring Work

Goal: ask AgentOS to run a recurring task without manually opening a chat.

```sh
agentos cron add \
  --every 1h \
  --text "Summarize important project updates" \
  --name hourly-project-check
```

Inspect jobs and runs:

```sh
agentos cron list
agentos cron status <job-id>
agentos cron runs <job-id>
```

Read next:

- [`operations.md`](operations.md)
- [`channels.md`](channels.md)
- [`scheduling.md`](scheduling.md)

## Publish a User-Visible Artifact

Goal: ask the agent to produce a file, report, slide deck, HTML page, image, or
media asset that you can inspect and share.

```sh
agentos agent -m "Create a short HTML report from the current notes"
agentos sessions export <session-key>
```

Read next:

- [`artifacts-and-media.md`](artifacts-and-media.md)
- [`features/skills.md`](features/skills.md)

## Recover From a Bad Run

Goal: understand what happened, reduce risk, and continue safely.

```sh
agentos doctor
agentos gateway status
agentos sessions show <session-key>
agentos cost
```

If a tool was denied or the agent had too much access:

```sh
agentos sandbox status
agentos agent --permissions restricted -m "Read only"
```

Read next:

- [`troubleshooting.md`](troubleshooting.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`approvals-and-permissions.md`](approvals-and-permissions.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)
- [`operations.md`](operations.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
