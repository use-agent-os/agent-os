# Configuration

AgentOS can be configured from the onboarding wizard, the Web UI setup
flow, CLI commands, environment variables, and TOML files. Use CLI commands for
routine setup and edit TOML only for advanced or scripted deployments.

## Config Load Order

AgentOS reads configuration in this order:

1. `AGENTOS_GATEWAY_CONFIG_PATH`
2. `./agentos.toml`
3. `~/.agentos/config.toml`
4. built-in defaults

Use `--config ./agentos.toml` when you want to write or inspect a
project-local config file.

## Secret Handling

Prefer environment-variable references for secrets:

```sh
export OPENROUTER_API_KEY="sk-..."
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
```

Avoid committing raw API keys to TOML files, shell history, examples, or issue
reports.

## First-Run Wizard

```sh
agentos onboard
```

Common options:

```sh
agentos onboard --if-needed
agentos onboard --minimal
agentos onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
agentos onboard --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
agentos onboard --provider ollama --model llama3.1
agentos onboard status
```

The router mode defaults to `recommended`. Use `--router disabled` when you want
direct single-model routing.

## Reconfigure One Section

The `configure` command edits a selected section:

```sh
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
agentos configure router --router recommended
agentos configure router --router openrouter-mix
agentos configure router --router disabled
agentos configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
agentos configure channels
agentos configure image-generation
agentos configure memory-embedding
```

Supported sections:

- `provider`
- `router`
- `channels`
- `search`
- `image-generation`
- `memory-embedding`

## Configuration Decision Table

| Need | Preferred command |
| --- | --- |
| First setup | `agentos onboard` |
| CI or install scripts | `agentos onboard --if-needed` |
| Change provider | `agentos configure provider ...` |
| Enable or disable routing | `agentos configure router ...` |
| Configure web search | `agentos configure search ...` |
| Configure messaging platforms | `agentos configure channels` |
| Inspect current values | `agentos config get` |
| Persist an advanced key | `agentos config set <key> <value> --config <path>` |

## Provider Configuration

Inspect provider support:

```sh
agentos providers list
agentos providers configure openrouter
agentos providers status
```

Onboarding-verified providers include:

- OpenRouter
- Bankr LLM Gateway
- OpenAI
- Anthropic
- Ollama
- DeepSeek
- Gemini
- DashScope / Qwen
- Moonshot AI
- Zhipu / Z.AI
- Baidu Qianfan
- Volcengine Ark

AgentOS also carries provider registry entries for additional
OpenAI-compatible or self-hosted backends. Use `agentos providers list` on
your install to see the current catalog.

Read: [`providers-and-models.md`](providers-and-models.md)

## Router Configuration

Router modes:

| Mode | Use when |
| --- | --- |
| `recommended` | You want the selected provider's default routing profile. |
| `openrouter-mix` | You want OpenRouter mixed-model defaults. |
| `disabled` | You want one configured provider/model for every turn. |

Commands:

```sh
agentos configure router --router recommended
agentos configure router --router openrouter-mix
agentos configure router --router disabled
```

Router-supported provider profiles depend on the installed build and configured
provider. Read [`features/agentos-router.md`](features/agentos-router.md) before
using direct model runs for evaluation.

### Router strategy

Independent of the mode above, `agentos_router.strategy` picks how the router
classifies each turn:

| Strategy | Default | Behavior |
| --- | --- | --- |
| `v4_phase3` | Yes | On-device ML router (BGE embeddings + LightGBM ensemble). No LLM call, nothing leaves the machine. The ~75MB model bundle is not distributed with the repo or the wheel yet, and the installers do not fetch it; when it's missing the router logs a warning at boot and pins every turn to the default tier (c1). Install its runtime dependencies with `uv sync --extra recommended` (or the `ml-router` extra), then restore the bundle into `src/agentos/agentos_router/models/v4.2_phase3_inference/` to enable per-turn routing — or use `llm_judge`, which needs no local model files. |
| `llm_judge` | No | Each turn is classified by a small LLM judge call instead of the local ML bundle. See "Local judge" below. |

```toml
[agentos_router]
strategy = "v4_phase3"   # or "llm_judge"
```

Both strategies are also selectable from the Mode dropdown in onboarding
(Web UI wizard and CLI): **AgentOS Router (Local ML)**, **AgentOS Router (LLM
Judge)**, or **Disabled**. The "Judge model" field only appears for the LLM
Judge strategy.

### Local judge (Ollama / LM Studio)

This section applies only when `agentos_router.strategy = "llm_judge"` — the
default `v4_phase3` strategy makes no LLM call and has no judge to configure.

With the `llm_judge` strategy, the router classifies each text turn with a
small LLM judge. Instead of a cloud model you can point the judge at a local
OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, vLLM) — no cloud
credentials required, and zero bytes added to the package.

In the interactive router setup, pick **Local endpoint** for the judge model and
supply the base URL (for example `http://localhost:11434/v1`) and the model
name. Onboarding validates the URL shape and runs one test classification call
to verify the endpoint is reachable.

Equivalent config (`[agentos_router]` in `agentos.toml`):

```toml
[agentos_router]
strategy    = "llm_judge"
judge_model = "llama3"
judge_base_url = "http://localhost:11434/v1"
# judge_api_key is optional — local endpoints usually accept any token; a
# placeholder is used when unset. It is redacted in logs.
judge_api_key = ""
```

`judge_base_url` only takes effect when `judge_model` is set. When it is, the
judge client is built against that endpoint and the usual "judge provider must
match `llm.provider`" constraint is bypassed. The resolved judge is logged at
boot (`router.judge_resolved` with `source="local"` and the base URL) and
reported by `agentos doctor`.

## Search Configuration

Inspect search providers:

```sh
agentos search list
agentos search status
agentos search query "AgentOS release notes"
```

Configure search:

```sh
agentos configure search --search-provider duckduckgo
agentos configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
```

Runtime-supported search providers in this build include Brave Search and
DuckDuckGo. Additional provider metadata may be present for future or
not-yet-runtime-supported integrations.

Read: [`search.md`](search.md)

## Channel Configuration

List supported channel types:

```sh
agentos channels types --json
agentos channels describe slack
agentos channels add telegram --name personal
agentos channels status
```

Channel saves update configuration. Restart the gateway after edits:

```sh
agentos gateway restart
agentos channels status <name> --json
```

See [`channels.md`](channels.md) for details.

## Memory Configuration

Useful commands:

```sh
agentos memory status
agentos memory index
agentos memory list
agentos memory search "project preference"
agentos memory show <path>
agentos memory dream
agentos memory flush-session <session-key>
```

Configure embedding behavior:

```sh
agentos configure memory-embedding
```

Memory can combine Markdown-backed sources with SQLite keyword and semantic
indexes. The exact memory shape depends on the configured provider and local
embedding support.

Read: [`features/memory.md`](features/memory.md)

## Sandbox and Permissions

Inspect or change posture:

```sh
agentos sandbox status
agentos sandbox on
agentos sandbox full
agentos sandbox bypass
agentos sandbox reset
```

Single-shot automation permissions:

```sh
agentos agent --permissions restricted -m "Read the repo and summarize it"
agentos agent --permissions full -m "Make a local patch and run tests"
```

For unattended automation that must stay inside a workspace:

```sh
agentos agent \
  --workspace /path/to/project \
  --workspace-lockdown \
  --scratch-dir /path/to/project/.scratch \
  -m "Investigate and propose the smallest fix"
```

Read: [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Gateway Binding

Foreground:

```sh
agentos gateway run --listen 127.0.0.1 --port 18791
```

Managed:

```sh
agentos gateway start --json
agentos gateway status
agentos gateway stop
agentos gateway restart
```

Bind precedence:

1. `--listen`
2. `--bind`
3. `AGENTOS_LISTEN`
4. `AGENTOS_GATEWAY_HOST`
5. config host
6. `127.0.0.1`

## Raw Config Editing

For advanced settings, inspect `agentos.toml.example` and edit the active
config file directly. Use CLI commands for routine provider, router, search,
channel, and sandbox changes because they avoid common key-shape mistakes.

After changing files by hand, restart the gateway and run:

```sh
agentos doctor
agentos gateway status
```

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
