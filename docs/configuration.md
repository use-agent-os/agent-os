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
- OpenCAP
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

### OpenCAP

Configure OpenCAP with its dedicated inference key:

```sh
export OPENCAP_API_KEY="ocap_..."
agentos configure provider --provider opencap --model oc-uncensored-1.0 \
  --api-key-env OPENCAP_API_KEY
```

The default base URL is `https://gw.capminal.ai/api/inference/v1` and can be
overridden with `OPENCAP_BASE_URL`. The default direct/fallback model is
`oc-uncensored-1.0`. Pilot Router's `recommended` mode selects the matching
multi-model OpenCAP profile. By default OpenCAP picks the cheapest eligible
upstream; optionally pin one model with:

```toml
[llm.provider_routing]
"glm-5.2" = "surplus"
```

### Ollama plain-text mode

AgentOS supports Ollama native tool calls. For a local model that does not
reliably implement Ollama's tool-call protocol, disable model-visible tools and
route directly to the configured model:

```toml
agent_max_iterations = 8

[llm]
provider = "ollama"
model = "qwen2.5:7b"
base_url = "http://localhost:11434"

[tools]
enabled = false

[agentos_router]
enabled = false
```

`tools.enabled = false` is a hard plain-text mode: no tool definitions are sent
to the provider and no tool handler is exposed for the turn. Keep a positive
`agent_max_iterations` when enabling tools on smaller local models so malformed
or repetitive tool calls terminate predictably.

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
| `pilot-v1` | Yes | English-optimized local ML router: an AgentOS-native, self-trained model (MiniLM embeddings + ONNX inference). Decides on-device with no LLM call and nothing leaves the machine. The bundle ships in the wheel under `src/agentos/agentos_router/models/pilot_v1/`; when it's missing (e.g. a source checkout without `git lfs pull`) the strategy tags the decision `pilot_unavailable` and routes the turn to the default tier (c1). Runtime deps are `numpy`/`onnxruntime`/`tokenizers` (in the `recommended` and `ml-router` extras); a minimal install without them degrades the same graceful way. Tunable via the `[agentos_router.pilot]` sub-table below. See [`features/agentos-router.md`](features/agentos-router.md#the-pilot-strategy) for status and upgrade notes. |
| `llm_judge` | No | Each turn is classified by a small LLM judge call instead of the local ML bundle. See "Local judge" below. |

```toml
[agentos_router]
strategy = "pilot-v1"   # default; or "llm_judge"
```

Router runtime dependencies (`onnxruntime`, `numpy`, `tokenizers`) stay in the
`recommended` / `ml-router` extras rather than the core install: a minimal
install without them does not fail — the router degrades to the default tier
and emits `pilot_unavailable` telemetry.

The supported strategies are also selectable from the Mode dropdown in
onboarding (Web UI wizard and CLI), a three-option selector: **Local ML —
English-optimized (Pilot)** (`pilot-v1`, the default), **Smart routing
(LLM-based)** (`llm_judge`), or **Off**. The legacy **Smart routing
(on-device)** (`v4_phase3`) option is no longer offered. The "Judge model" field
only appears for the LLM-based strategy; the "Pilot safety net" field only
appears for the Pilot strategy.

#### Upgrading from v4_phase3

Historical onboarding persisted `strategy = "v4_phase3"` explicitly in
`~/.agentos/config.toml`. The default has since flipped to `pilot-v1`, so on the
next config load AgentOS **automatically migrates** any config still pinning
`v4_phase3`:

- the strategy is rewritten to `pilot-v1`;
- the original file is backed up verbatim next to it as
  `config.toml.backup.<timestamp>` (mode `0600`);
- the flip is logged, and the rewrite is idempotent (a config already on
  `pilot-v1` is left untouched, with no backup).

There is no way to keep `v4_phase3` in config — the legacy engine and its
model bundle were removed from the tree (Phase C), onboarding no longer offers
it as a Mode option, and a value that bypasses the file migration (e.g. an env
override) normalizes to `pilot-v1` at config load.

#### Pilot strategy settings

When `strategy = "pilot-v1"`, the optional `[agentos_router.pilot]` sub-table
tunes the Pilot router:

```toml
[agentos_router]
strategy = "pilot-v1"

[agentos_router.pilot]
# Under-routing safety-net floor (0.0–1.0). The effective cutoff is
# max(safety_net_threshold, router.confidence_threshold), so a value below the
# confidence threshold has no effect. Default 0.5.
safety_net_threshold = 0.5

# Override the Pilot artifact directory. Defaults to the bundled
# `models/pilot_v1/` root; set this to point at a bundle elsewhere on disk.
# pilot_artifact_dir = "~/pilot_v1"
```

### Local judge (Ollama / LM Studio)

This section applies only when `agentos_router.strategy = "llm_judge"` — the
default `pilot-v1` local strategy makes no LLM call
and have no judge to configure.

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
agentos gateway status   # shows both the CLI and running-gateway versions
agentos gateway stop
agentos gateway restart
```

`agentos gateway status` reports the installed CLI version and the running
gateway's version; when they differ it appends a mismatch line advising a
restart (typically after `agentos upgrade --no-restart` or a manual package
upgrade).

Bind precedence:

1. `--listen`
2. `--bind`
3. `AGENTOS_LISTEN`
4. `AGENTOS_GATEWAY_HOST`
5. config host
6. `127.0.0.1`

## Update Notifications

On commands that connect to the gateway, the CLI checks PyPI at most once every
24h and, if a newer release of `use-agent-os` exists, prints a one-line notice
on stderr suggesting `agentos upgrade`. The check is silent on failure and is
suppressed on non-interactive runs (no TTY) and in CI.

```toml
[updates]
notify = true   # set false to silence the "new release available" notice
```

`updates.notify` defaults to `true`. Set it from the setup UI (Finish step →
Updates), with `agentos config` (a `config.patch` on `updates.notify`), or by
editing the config file directly. The state file
`~/.agentos/state/update_notice.json` tracks the last check time for
throttling; delete it to force a re-check. To silence the notice for a single
run without changing config, set `AGENTOS_NO_UPDATE_NOTICE=1`.

Related: `agentos upgrade` (the primary upgrade path), the version-skew policy,
and the `AGENTOS_ALLOW_VERSION_SKEW=1` escape hatch are documented in the
[README Upgrade section](../README.md#upgrade).

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
