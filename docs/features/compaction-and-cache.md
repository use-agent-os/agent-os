# Compaction and Cache Continuity

Long agent sessions need context management. AgentOS uses compaction,
bounded history, tool-result projection, and cache-aware prompt placement to
keep long-running tasks moving.

Compaction is separate from memory. Memory is durable recall. Compaction is an
active-session continuity tool.

## What Compaction Does

When session history approaches the configured context budget, AgentOS can
compact older transcript entries into a durable summary and keep the recent
tail active.

The goal is to preserve:

- user goal;
- current status;
- open steps;
- changed files and artifacts;
- known failures;
- important tool results;
- next action.

Compaction is not a guarantee that every old word remains model-visible. Export
sessions or save files when exact historical text matters.

## User-Visible Lifecycle

Depending on surface and trigger, users may see:

- compaction started;
- compaction skipped;
- compaction completed;
- compaction failed.

When no compaction is needed, AgentOS uses this stable message:

```text
Already within context budget; no compact was applied
```

That message is a no-op, not a failure.

A compaction inside a turn records what triggered it, in the session summary's
`trigger_reason` and in the lifecycle notice's `phase`: `agent_inline_preflight`
(the request was about to exceed the threshold), `agent_inline_overflow` (the
provider refused the request as too large) or `agent_inline_timeout` (a large
request timed out twice; see
[Retrying transient failures](../providers-and-models.md#retrying-transient-failures)).
Records written before this distinction, and compactions with no known trigger,
say `agent_inline_overflow`. Each compaction decision is also logged as
`compaction.decision` with its trigger: at info level when it compacted or
failed, at debug level when the request was simply under the threshold.

## When to Compact Manually

Manual compaction is useful when:

- the session is long and you are about to start a new phase;
- a previous tool-heavy turn produced a lot of context;
- the UI indicates context pressure;
- you want the next answer to focus on the current state rather than the whole
  transcript.

Avoid compact loops when the runtime says the session is already within budget.

## Passive Compaction

Passive compaction happens when the next model request would take more than
85% of the model's context window (`preflight_compact_ratio` in
`agentos.toml`, default `0.85`). The check runs before every model call -- the
first one of a turn included -- and never after a reply has streamed, so
compacting does not make the model answer the same step twice. It also never
runs while a tool call is waiting for its result.

A compaction replaces everything before the current turn with one summary,
written by the session's own model. The current turn -- your message, its
attachments and every tool step so far -- stays word for word. Only when the
current turn alone is too large are its oldest finished tool steps folded into
the summary as well; your message and the latest step are always kept.

If passive compaction fails, the safest user response is usually:

1. let the current turn finish or fail cleanly;
2. export the session if exact history matters;
3. retry with a narrower request or manually save key artifacts;
4. enable diagnostics if the failure repeats.

## Prompt Cache Continuity

Prompt caching works best when stable prompt parts stay stable. AgentOS keeps
the conversation append-only: once a message has been sent to the model it is
replayed byte for byte on every later request, in the same turn and in later
turns. In practice:

- the system prompt and tool definitions come first and stay the same between
  requests (workspace files and project knowledge are part of that cached
  system prompt); loading a deferred tool with `tool_search` appends its
  definition, which costs one cache write for the tool list;
- the current time, the skills list and other per-turn context (memory
  recall, plan mode, ...) are added at the end of your message for that turn
  and stay there; each part is only repeated when it changes;
- a tool result too large for the request is shortened once, before the model
  first sees it; when the turn ends, results and tool-call arguments longer
  than `[compaction] history_tool_result_max_chars` (default: 2% of the
  context window, 4k-12k characters) are kept as head/tail plus a handle to
  the full output for later turns. That happens once per turn, so the next
  request re-caches only the end of the previous turn;
- compaction is the only thing that rewrites earlier context;
- model/provider switches stay visible through diagnostics when they may
  affect cache continuity.

Cache continuity is best-effort. Routing, tools, attachments, provider changes,
or a large new context can reduce cache reuse.

### Prompt Cache Configuration

Control prompt caching behavior via `agentos.toml`:

```toml
[prompt_cache]
mode = "auto"   # "auto" | "on" | "off"
```

- `auto` (default): Enables prompt prefix caching when supported by the provider and model.
- `on`: Forces prompt caching on for all requests.
- `off`: Disables prompt caching.

Environment variable override: `AGENTOS_CACHE_MODE=auto|on|off`. The legacy `prompt_cache.enabled` config key and `AGENTOS_CACHE_ENABLED` environment variable are deprecated and mapped to `mode` (`on`/`off`) automatically.

## Related Commands and Surfaces

Manual compaction is primarily surfaced in chat and Web UI flows. For
inspection and recovery:

```sh
agentos sessions show <session-key>
agentos sessions export <session-key>
agentos diagnostics on
```

Sessions whose memory never landed are visible in `agentos doctor`. Automatic
repair was removed; the raw transcripts remain on disk:

```bash
agentos memory raw-fallbacks list
```

## Best Practices

- Keep important final artifacts in files or published artifacts.
- Use memory for durable preferences and reusable project facts.
- Use session export for exact old transcripts.
- Use manual compaction before a new phase in a very long session.
- Do not repeatedly compact a short or already-within-budget session.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
