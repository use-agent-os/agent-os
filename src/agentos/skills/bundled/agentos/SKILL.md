---
name: agentos
description: "Operate and configure AgentOS itself: the `agentos` CLI, agentos.toml, gateway/Web UI, providers and models, skills, channels, sessions, cron, sandbox, and memory. Use when: (1) the user asks to change an AgentOS setting (model, provider, router tier, auth, channels, search), (2) starting, stopping, or debugging the gateway or Web UI, (3) installing, updating, or removing skills and taps, (4) inspecting sessions, usage/cost, cron jobs, or diagnostics, (5) migrating from another agent runtime. NOT for: authoring new skills (see docs/features/skills.md), operating other agent CLIs, or modifying AgentOS source code."
always: false
triggers:
  - agentos
  - config.toml
  - agentos.toml
  - gateway
  - provider
  - "change model"
  - "install skill"
  - onboard
  - doctor
provenance:
  origin: agentos-original
  license: MIT
metadata:
  agentos:
    emoji: "🧭"
---

# AgentOS self-operation

AgentOS is the agent runtime you are (or may be) running inside: a Python
framework (`pip install use-agent-os`) with a Typer CLI (`agentos`), a local
gateway server with a Web UI, messaging channels, a skills system, memory,
cron, and a model router. This skill teaches you to configure and operate
AgentOS on the user's behalf through its CLI and config file.

Docs: local `docs/` in the repo, published at https://useagentos.dev/docs/

## Scope & verification — read first

This skill is a condensed operating guide, not the full source of truth.
Rules that prevent broken commands:

- **Never invent flags or subcommands.** If a flag is not written here, run
  `agentos <command> --help` first and use what it prints.
- Command *groups* are exact: it is `agentos providers configure`, not
  `agentos configure provider`; `agentos migrate hermes`, not
  `agentos hermes`.
- If a command is missing from this skill, that is not evidence it does not
  exist — check `agentos --help` before answering "impossible".
- Config keys go through `agentos config get <dot.key>` / `config set` —
  verify a key exists with `config get` before setting it.

## Quick start

```sh
agentos onboard              # first-run setup wizard (or: agentos init, agentos configure)
agentos doctor               # diagnose readiness, print recovery steps
agentos gateway run          # gateway + Web UI, foreground (default port 18791)
agentos chat                 # interactive terminal chat
agentos agent -m "..."       # one-shot, automation-friendly agent turn
```

### `agentos chat` REPL essentials

The interactive REPL exposes slash commands; the most-used are `/new [title]`,
`/resume <key>`, `/status`, `/model <id>`, `/clear`, `/compact`, `/cost`,
`/save [path]`, `/help`, and `/exit`. Pilot Router tier pins are available
in both gateway and `--standalone` modes:

- `/c0` … `/c3` — pin the Pilot Router to a configured tier for this session.
  The active tier shows in the bottom toolbar (e.g. `tier:c3`) and in
  `/status` until you run `/auto`, exit, or the hold expires.
- `/auto` — restore automatic Pilot Router routing (clear the pin).

The assistant speaker label on the `◢` marker defaults to `agentos`; override
with the `AGENTOS_ASSISTANT_LABEL` env var. The active input row is framed by a
top and bottom rule so it reads as a distinct box; the bottom toolbar renders
`title · model · [tier:cN]` while typing, with the title sourced from
`/new <title>` or loaded on `/resume`.

## CLI map

Top-level: `init`, `onboard`, `configure`, `doctor`, `upgrade`, `chat`,
`agent`, `reset`, plus these groups (each supports `--help`):

| Group | Subcommands |
| --- | --- |
| `gateway` | `run`, `start`, `status`, `stop`, `restart` (`--port`, `--bind`, `--listen`, `--config`, `--json`, `--debug`) |
| `config` | `get [key]` (empty key = show all), `set <dot.key> <value>` |
| `providers` | `list`, `status`, `configure <id> [-m MODEL] [-k API_KEY] [--base-url] [--proxy]` |
| `models` | `list` |
| `skills` | `list`, `search`, `view`, `install`, `uninstall`, `update`, `publish`, `tap add/list/remove` |
| `sessions` | `list`, `show`, `resume`, `abort`, `delete`, `export` |
| `cron` | `list`, `status`, `add`, `update`, `remove`, `run`, `runs` |
| `channels` | `list`, `status`, `types`, `describe`, `add`, `remove`, `enable`, `disable`, `edit`, `restart`, `logout`, `pairing …` |
| `memory` | `status`, `index`, `list`, `search`, `show`, `dream`, `embedding-download`, `repair …`, `raw-fallbacks …` |
| `sandbox` | `status`, `on`, `bypass`, `full`, `reset` |
| `search` | `list`, `status`, `query`, `configure` |
| `cost` | usage and estimated cost report |
| `diagnostics` | `status`, `on`, `off` |
| `migrate` | `openclaw`, `hermes` (`--source`, `--profile`, `--apply`, `--migrate-secrets`; dry-run without `--apply`) |
| `agents` | `list`, `add`, `delete` (durable agents) |
| `mcp-server` | `run` (MCP bridge) |
| `replay`, `dist`, `onboard` | replay recorded turns / workspace inventory / setup status |

## Configuration

File resolution (highest precedence first):

1. Environment variables
2. `./agentos.toml` (current directory)
3. `~/.agentos/config.toml` (user-global; `AGENTOS_STATE_DIR` moves `~/.agentos`)
4. Built-in defaults

Most commands accept `--config <path>` to target a specific file. On load,
AgentOS auto-migrates outdated config schemas and writes a backup next to
the file before rewriting it.

Main `agentos.toml` sections (full commented reference:
`agentos.toml.example` in the repo):

| Section | Controls |
| --- | --- |
| top-level | `workspace_dir`, `state_dir`, logging, `search_provider`/`search_api_key`, timeouts |
| `[llm]` | `provider`, `model`, `api_key`, `base_url`, `proxy`, `[llm.provider_routing]` |
| `[agentos_router]` | router on/off, `strategy` (`pilot-v1`), tier settings under `[agentos_router.tiers.c0..c3]` |
| `[skills]` | skill filtering/injection: `filter_strategy`, `filter_top_k`, `injection_mode` |
| `[memory]` | memory source and embedding model, `[memory.dream]` |
| `[sandbox]` | `sandbox`, `default_level` (DISABLED/STANDARD/STRICT/LOCKED), `backend`, network/mounts |
| `[permissions]` | `default_mode` = `off` \| `on` \| `bypass` \| `full` (pair with `agentos sandbox …`) |
| `[auth]` | gateway auth: `mode` (`none`/`token`/`password`), `token`, `allow_unauthenticated_public` |
| `[control_ui]` | `allowed_origins` for reverse-proxy setups |
| `[updates]` | `notify` (default true) — the once-per-24h "new release available" notice |
| `[channels]` | messaging channels (`[[channels.channels]]` entries) |
| `[compaction]`, `[agent_token_saving]`, `[task_runtime]` | context compaction, tool-result projection, concurrency |

## Common operations (verified recipes)

**Change the model/provider (persistently):**

```sh
agentos providers configure openrouter -m anthropic/claude-sonnet-4   # provider + model in one step
agentos config set llm.model "anthropic/claude-sonnet-4"              # just the model key
agentos configure                                                     # interactive wizard
agentos gateway restart                                               # apply to a running gateway
```

**Gateway lifecycle:**

```sh
agentos gateway start          # background; `run` = foreground
agentos gateway start --port 9000   # `run`/`start` share --port/--bind/--listen
agentos gateway status --json  # machine-readable status
agentos gateway stop
```

Default port **18791**, loopback bind. `--listen HOST:PORT` overrides
`--bind`/`--port` together. `gateway status` (and `--json`) reports **both**
the installed CLI version (`cliVersion`) and the running gateway's version
(`gatewayVersion`); a `versionMismatch` diagnostic means the gateway is running
old code — restart it.

**Upgrading AgentOS:**

```sh
agentos upgrade                # upgrade, then restart + verify the gateway
agentos upgrade --check        # is a newer release available? changes nothing
agentos upgrade --dry-run      # print the command that would run; touch nothing
agentos upgrade --no-restart   # upgrade only; gateway keeps running OLD code
```

`agentos upgrade` is the primary path: it detects the install method and
delegates (`uv tool upgrade` / `pipx upgrade`), then by default restarts the
managed gateway and verifies it reports the new version before declaring
success. For pip / editable / source installs it prints the exact manual
command and exits non-zero (**exit 3**) rather than faking it; a failed or
unverifiable upgrade is **exit 1**. Flags: `--timeout` (subprocess bound,
default 600s; kills the process group on timeout), `--config`, `--json`.

Commands that reach the gateway compare CLI and gateway versions: a gateway
**older** than the CLI warns (post-upgrade, before restart); a gateway
**newer** than the CLI is *refused* (schema-corruption risk) unless
`AGENTOS_ALLOW_VERSION_SKEW=1`. On gateway-connected commands the CLI also
prints a once-per-24h "new release available" notice on stderr (TTY only, not
in CI); silence it with `updates.notify = false` or `AGENTOS_NO_UPDATE_NOTICE=1`.

**Public / LAN bind (security-gated):** with `auth.mode = "none"` the
gateway *refuses* non-loopback binds by design. The right fix is enabling
auth, not bypassing the guard:

```sh
agentos config set auth.mode token
agentos config set auth.token "<long random secret>"
agentos gateway restart
```

Only set `auth.allow_unauthenticated_public = true` when an external layer
(reverse-proxy auth, VPN, firewall) already gates access. Behind a reverse
proxy on another origin, also set `control_ui.allowed_origins`.

**Skills:**

```sh
agentos skills list                    # installed, per layer
agentos skills search <query>
agentos skills install <name>                    # from ClawHub (default source)
agentos skills install owner/repo:path -s github # from a GitHub repo/URL
agentos skills tap add owner/repo      # register a GitHub repo as a skill source
agentos skills tap list
agentos skills update
agentos skills uninstall <name>
```

Skill layers (later overrides earlier): `extra` (config dirs) → `bundled`
(shipped) → `managed` (`~/.agentos/skills`, where installs land) →
`personal` (`~/.agents/skills`) → `project` (`<workspace>/.agents/skills`)
→ `workspace` (`<workspace>/skills`).

**One-shot automation:**

```sh
agentos agent -m "summarize README.md" --model gpt-5.4-mini --timeout 120
agentos agent --json -m "Return a machine-readable summary"
agentos agent --workspace /path --workspace-strict -m "Inspect this repo"
```

Bounding flags: `--timeout` (wall-clock seconds), `--max-iterations`,
`--iteration-timeout-seconds`, `--tool-timeout-seconds`; containment:
`--workspace-strict` (reads), `--workspace-lockdown` (writes),
`--scratch-dir`.

**Day-two operations:**

```sh
agentos sessions list / show <id> / export <id> <out>
agentos cron list / add / run <id> / runs
agentos cost                   # usage + estimated spend
agentos diagnostics on         # runtime diagnostics logging
agentos migrate hermes --source <dir> [--apply]   # dry-run first, then --apply
```

## Gateway HTTP API

The gateway is also a REST + WebSocket server on port 18791:
`GET /api/config|sessions|agents|cron|usage|system/status|channels/status`,
`POST /api/chat`, `GET /api/chat/history`, approvals endpoints under
`/api/approvals*`, and `WS /ws` (primary RPC transport). On loopback binds
auth is optional; on public binds the `[auth]` token gates every request.
Full reference: `docs/http-api.md` (https://useagentos.dev/docs/http-api).

## Key paths

| Path | What |
| --- | --- |
| `~/.agentos/` | state root (override: `AGENTOS_STATE_DIR`) |
| `~/.agentos/config.toml` | user-global config |
| `./agentos.toml` | project-local config (wins over global) |
| `~/.agentos/skills/` | managed skills (installed via `skills install`) |
| `~/.agentos/skills-taps.json` | registered skill taps |
| `~/.agents/skills/` | personal skills layer |

## Troubleshooting

- **Anything broken** → `agentos doctor` first; it prints recovery steps.
- **Setting doesn't take effect** → confirm which file won:
  `agentos config get <key>`; remember `./agentos.toml` beats
  `~/.agentos/config.toml`; restart the gateway after edits.
- **Gateway won't bind publicly** → intentional auth guard; see the
  public-bind recipe above.
- **Provider/model errors** → `agentos providers status`,
  `agentos models list`, then `agentos providers configure …`.
- **Skill missing from prompt** → `agentos skills list` (check layer and
  enablement), then `[skills]` filter settings (`filter_top_k`,
  `filter_strategy`).
- **Deep debugging** → `agentos diagnostics on`, reproduce, then
  `agentos replay` on the recorded turn.

## Docs map

`docs/README.md` is the index; per-topic pages mirror to
`https://useagentos.dev/docs/<page>`: `quickstart`, `cli`, `configuration`,
`gateway`, `http-api`, `providers-and-models`, `channels`, `operations`,
`scheduling`, `sessions`, `usage-and-cost`, `tools-and-sandbox`,
`approvals-and-permissions`, `mcp-server`, `troubleshooting`, and
`features/skills`, `features/agentos-router`, `features/memory`.
