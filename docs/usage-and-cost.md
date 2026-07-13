# Usage and Cost

AgentOS records token usage and estimated cost from the running gateway.
Use the cost view after routed, tool-heavy, channel, or long-context work to
understand where model spend is going.

## Requirements

Cost inspection uses the gateway:

```sh
agentos gateway status
```

If the gateway is not running:

```sh
agentos gateway run
```

## Show Cost

```sh
agentos cost
```

The default view lists session/model rows with input tokens, output tokens, and
estimated cost.

## Group by Model

```sh
agentos cost --by-model
```

Use this when AgentOS Router is enabled and you want to see which models carried
the recent workload.

## Use JSON Output

```sh
agentos cost --json
agentos cost --by-model --json
```

JSON output is useful for local dashboards, regression checks, and automated
reports.

## What to Check First

| Signal | What it can mean |
| --- | --- |
| Many rows for premium models | Router policy or task shape may be escalating more often than expected. |
| High input tokens | Long history, large tool results, or large prompt/tool schema surfaces may dominate cost. |
| High output tokens | The task may need tighter instructions or a smaller response format. |
| Cost concentrated in one session | Inspect that session before changing global configuration. |

## Lower Cost Safely

Start with router and diagnostics:

```sh
agentos configure router --router recommended
agentos diagnostics on
agentos cost --by-model
```

For large tool results, read:

- [`features/tool-compression.md`](features/tool-compression.md)
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

For simple one-shot automation, bound the run:

```sh
agentos agent --max-iterations 20 --timeout 600 -m "Bounded task"
```

## Notes and Limits

- Cost is an estimate based on recorded runtime usage and configured pricing.
- Provider bills remain the source of truth for actual charges.
- Tool compression and routing can reduce model context cost, but they should
  be checked against task success, not only token totals.
- Diagnostics can explain why a turn routed, compacted, retried, or produced
  unusually large outputs.

Read next:

- [`features/agentos-router.md`](features/agentos-router.md)
- [`features/tool-compression.md`](features/tool-compression.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
