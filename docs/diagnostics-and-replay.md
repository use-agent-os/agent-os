# Diagnostics and Replay

Diagnostics and replay help explain what happened during an AgentOS turn.
Use them when a result was surprising, slow, expensive, interrupted, or hard to
reproduce from the chat transcript alone.

## Diagnostics

Diagnostics are runtime logging controls exposed through the gateway.

Check status:

```sh
agentos diagnostics status
agentos diagnostics status --json
```

Enable diagnostics:

```sh
agentos diagnostics on
```

Enable raw turn-call capture when a maintainer asks for deeper provider request
evidence:

```sh
agentos diagnostics on --raw
```

Turn diagnostics off after collecting enough evidence:

```sh
agentos diagnostics off
```

## When to Use Diagnostics

Use diagnostics for:

- provider retries, timeouts, or empty responses;
- Pilot Router model decisions;
- prompt-cache or cache-break investigation;
- compaction lifecycle events;
- large tool-result compression;
- channel delivery failures;
- unusually high cost or latency.

Avoid leaving raw diagnostics on longer than needed. Raw captures may contain
private prompts, tool outputs, local paths, or provider-visible content.

## Replay a Recorded Turn

Replay reads a recorded turn from the decision log and prints a human-readable
transcript. It is read-only: it does not re-run tools.

```sh
agentos replay --session <session-key> --turn <turn-id>
```

Use replay when:

- a chat has moved on but you need to inspect an earlier turn;
- a bug report needs concise reproduction evidence;
- you want to compare transcript output with diagnostics and cost data.

## Pair Replay With Sessions

Find the session first:

```sh
agentos sessions list
agentos sessions show <session-key>
```

Export the full session if exact context matters:

```sh
agentos sessions export <session-key> --output session.md
```

## Safe Sharing

Before sharing diagnostics, replay output, or exported sessions publicly,
remove:

- provider keys and bearer tokens;
- private local paths;
- private channel identifiers;
- customer, project, or account names that should not be public;
- raw provider prompts or tool outputs that include confidential content.

Read next:

- [`sessions.md`](sessions.md)
- [`usage-and-cost.md`](usage-and-cost.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
