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

Use this when Pilot Router is enabled and you want to see which models carried
the recent workload.

## Use JSON Output

```sh
agentos cost --json
agentos cost --by-model --json
```

JSON output is useful for local dashboards, regression checks, and automated
reports.

## Report What Routing Saved

```sh
agentos cost savings
agentos cost savings --start-date 2026-08-01 --end-date 2026-08-31
agentos cost savings --pdf ~/pilot-router-savings.pdf
```

`agentos cost` shows what you spent; `agentos cost savings` shows what the
[Pilot Router](features/agentos-router.md) avoided spending. It reads the local
decision log instead of the gateway, so it works with the gateway stopped, and
`--pdf` writes a branded one-page report you can send on.

The baseline is the most expensive model configured in `[router.tiers]` — what
every routed turn would have cost on your top tier — priced on input tokens
only at list rates. That makes the figure a floor, not a full-turn saving, and
it covers routing alone: tool-result projection, short-reply enforcement,
prompt-cache hits and thinking mode are excluded. See
[`cli.md`](cli.md#agentos-cost-savings) for the full option list.

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

## Cap Spend with a Hard Stop

Cost views are after the fact. To bound spend while it happens, set ceilings in
`[budgets]`:

```toml
[budgets]
session_limit = 5.0
daily_limit = 50.0
daily_warn = 40.0
```

A turn that starts at or above a hard limit is refused before any provider call
with a `budget_exceeded` error (turn outcome `budgetLimited`), and ceilings are re-checked between iterations
so a long tool loop stops at the ceiling too. A `*_warn` threshold raises a
one-shot warning without stopping the turn. Spend is persisted to
`~/.agentos/state/spend_ledger.db`, so a ceiling survives a gateway restart.
Per-agent and per-channel ceilings use `[budgets.agent_daily_limit]` and
`[budgets.channel_daily_limit]`.

Each admitted turn also holds `budgets.turn_reservation` dollars (default
`0.25`) of headroom until it ends, so a concurrent subagent fan-out cannot
clear one ceiling several times over against the same snapshot.

See [`configuration.md`](configuration.md#spend-budgets) for the full key list.

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
