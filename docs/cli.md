# CLI Reference

The `agentos` CLI is the fastest way to configure, run, inspect, and
automate AgentOS.

Run:

```sh
agentos --help
agentos <command> --help
```

## Main Commands

| Command | Purpose |
| --- | --- |
| `agentos init` | Initialize a workspace. |
| `agentos doctor` | Diagnose readiness and print recovery steps. |
| `agentos onboard` | Run or inspect first-run setup. |
| `agentos configure` | Reconfigure provider, router, channels, search, image generation, or memory embedding. |
| `agentos gateway` | Run and manage the gateway server. |
| `agentos chat` | Start interactive terminal chat. |
| `agentos agent` | Run a single automation-friendly agent turn. |
| `agentos sessions` | List, inspect, resume, abort, delete, or export sessions. |
| `agentos skills` | List, search, view, install, update, publish, and inspect skills. |
| `agentos memory` | Inspect and maintain memory. |
| `agentos channels` | Configure and inspect messaging channels. |
| `agentos providers` | Configure and inspect LLM providers. |
| `agentos search` | Configure and use web search. |
| `agentos sandbox` | Inspect or change default sandbox posture. |
| `agentos cron` | Manage scheduled AgentOS runs. |
| `agentos cost` | Inspect usage and estimated cost. |
| `agentos diagnostics` | Enable or disable runtime diagnostics logging. |
| `agentos replay` | Replay a recorded turn from the decision log. |
| `agentos migrate` | Import state from external agent runtimes. |
| `agentos models` | Inspect available models. |
| `agentos agents` | Manage durable agents. |
| `agentos mcp-server` | Run the AgentOS MCP server bridge. |
| `agentos dist` | Emit a reproducible workspace-state inventory. |
| `agentos reset` | Reset a session and flush memory synchronously. |

## Run Surfaces

Web UI and gateway:

```sh
agentos gateway run
agentos gateway start --json
agentos gateway status
agentos gateway restart
agentos gateway stop
```

Terminal chat:

```sh
agentos chat
agentos chat --model gpt-5.4-mini
agentos chat --session <session-key>
agentos chat --standalone --workspace /path/to/project
```

One-shot automation:

```sh
agentos agent -m "Review the current directory"
agentos agent --json -m "Return a short machine-readable summary"
agentos agent --workspace /path/to/project --workspace-strict -m "Inspect this repo"
agentos agent --timeout 600 --max-iterations 30 -m "Run a bounded investigation"
```

Useful automation flags:

| Flag | Purpose |
| --- | --- |
| `--workspace` | Set the workspace root. |
| `--workspace-strict` | Restrict read-side file tools to the workspace. |
| `--workspace-lockdown` | Contain writes to workspace or scratch directory. |
| `--scratch-dir` | Place temporary scripts/logs/candidate patches in a known directory. |
| `--timeout` | Set total agent wall-clock timeout. |
| `--max-iterations` | Bound the model/tool loop. |
| `--max-provider-retries` | Bound transient provider retries. |
| `--length-capped-continuations` | Bound automatic continuations after length-limited provider output. |
| `--thinking` | Override reasoning level. |
| `--permissions` | Select restricted, bypass, or full permission posture. |
| `--transcript-path` | Write a JSONL transcript for automation. |
| `--usage-path` | Write usage JSON. |
| `--session-db-path` | Persist session replay across invocations. |

## Configuration Commands

Provider and router:

```sh
agentos onboard
agentos onboard status
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
agentos configure router --router recommended
agentos providers list
agentos providers configure openrouter
agentos providers status
```

Search:

```sh
agentos search list
agentos search configure duckduckgo
agentos search query "latest AgentOS release"
agentos configure search --search-provider duckduckgo
```

Channels:

```sh
agentos channels types
agentos channels describe telegram
agentos channels add telegram --name personal
agentos channels list
agentos channels status
agentos channels enable personal
agentos channels disable personal
agentos channels restart personal
agentos channels remove personal
```

Raw config:

```sh
agentos config get llm.provider
agentos config set gateway.port 18791
```

More detail:

- [`configuration.md`](configuration.md)
- [`providers-and-models.md`](providers-and-models.md)
- [`search.md`](search.md)
- [`channels.md`](channels.md)

## Skills

```sh
agentos skills list
agentos skills search pdf
agentos skills view pdf-toolkit
agentos skills install <skill-name>
agentos skills update --all
agentos skills uninstall <skill-name>
```

Read:

- [`features/skills.md`](features/skills.md)

## Sessions and History

```sh
agentos sessions list
agentos sessions show <session-key>
agentos sessions resume <session-key>
agentos sessions abort <session-key>
agentos sessions export <session-key>
agentos sessions delete <session-key>
```

Read: [`sessions.md`](sessions.md)

## Memory

```sh
agentos memory status
agentos memory index
agentos memory list
agentos memory search "preference"
agentos memory show <path>
agentos memory dream
agentos memory flush-session <session-key>
agentos memory repair list
agentos memory raw-fallbacks list
```

Read: [`features/memory.md`](features/memory.md)

## Durable Agents and Scheduling

```sh
agentos agents list
agentos agents add research --name Research --workspace /path/to/research
agentos agents delete research
agentos cron list
agentos cron add --every 1h --text "Summarize important updates" --name hourly-summary
agentos cron status <job-id>
agentos cron runs <job-id>
```

Read:

- [`agents.md`](agents.md)
- [`scheduling.md`](scheduling.md)

## Cost, Diagnostics, and Replay

```sh
agentos cost
agentos diagnostics status
agentos diagnostics on
agentos diagnostics off
agentos replay --session <session-key> --turn <turn-id>
```

Use diagnostics and replay when you need to understand why a turn behaved a
certain way.

Read:

- [`usage-and-cost.md`](usage-and-cost.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)

## MCP Server Bridge

```sh
agentos mcp-server run
agentos mcp-server run --gateway ws://localhost:18792/ws
```

Read: [`mcp-server.md`](mcp-server.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
