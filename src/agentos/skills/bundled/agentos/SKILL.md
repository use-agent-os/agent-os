---
name: agentos
description: "Operate and configure AgentOS itself: the `agentos` CLI, agentos.toml, gateway/Web UI, providers and models, web search, X (Twitter) search via xAI (`x_search`; SuperGrok login or XAI_API_KEY), skills, channels, sessions, projects (session groups with shared knowledge), ACP coding harnesses (Claude Code, Codex, Gemini CLI, ... driven over the Agent Client Protocol), cron, sandbox, and memory. Use when: (1) the user asks to change an AgentOS setting or turn a capability on (model, provider, router tier, auth/login, channels, search), (2) starting, stopping, or debugging the gateway or Web UI, (3) installing, updating, or removing skills and taps, (4) inspecting sessions, usage/cost, cron jobs, or diagnostics, (5) migrating from another agent runtime. NOT for: authoring new skills (see docs/features/skills.md), delegating a coding task to Claude Code / Codex / another harness (use the sub-agent skill), or modifying AgentOS source code."
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
  - x search
  - x_search
  - twitter
  - xai
  - supergrok
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
`/resume <key>`, `/status`, `/model [filter]`, `/use <model-id>`, `/clear`, `/compact`, `/cost`,
`/save [path]`, `/help`, and `/exit`. Pilot Router tier pins are available
in both gateway and `--standalone` modes:

- `/c0` … `/c3` — pin the Pilot Router to a configured tier for this session.
  The active tier shows in the bottom toolbar (e.g. `tier:c3`) and in
  `/status` until you run `/auto` or exit. The pin is sticky: it does not time
  out, so a forgotten `/c3` keeps billing at `c3`.
- `/use <model-id>` — pin the route to a specific model instead of a tier. Only
  the active provider's models can be pinned, since every turn runs through the
  single configured `llm.provider`; the model inherits the default tier's
  thinking level and pricing baseline. Distinct from `/model`, whose argument
  filters the listing rather than switching.
- `/auto` — restore automatic Pilot Router routing (clear the pin).
- `/plan [off]` — toggle plan mode (gateway mode only): the session becomes
  research-only until the plan the agent presents via `exit_plan_mode` is
  approved (Web UI plan card, or `/plan off` + telling it to proceed).

A pin withdraws the model's own `router_control` tool, so it cannot route
around the choice. Image turns still go to a vision tier (they are routed
before pins apply), and a pinned turn skips the large-context tier floor — it
fails at the provider rather than being upgraded if the context outgrows the
pinned model. In the Web UI the same pin is set from the route picker in the
chat composer, which also shows which tier automatic routing last chose.

The assistant speaker label on the `◢` marker defaults to `agentos`; override
with the `AGENTOS_ASSISTANT_LABEL` env var. The active input row is framed by a
top and bottom rule so it reads as a distinct box; the bottom toolbar renders
`title · model · [tier:cN]` while typing, with the title sourced from
`/new <title>` or loaded on `/resume`. `agentos chat` runs full-screen by
default: the conversation renders in a scrollable pane above the pinned input
frame (the branded welcome screen shows at the top on launch), so the frame
stays visible while the assistant streams (`PgUp`/`PgDn` scroll history). The
mouse wheel also scrolls when the pointer is over the transcript. Dragging the
left mouse button across the transcript highlights a span in reverse video;
releasing the button copies the plain text (ANSI stripped, CJK width-aware) to
the system clipboard (`pbcopy` / `wl-copy` / `xclip` / `xsel` / `clip`, with
an OSC 52 fallback). Click again to clear the selection. The assistant's
streamed reply is styled inline as it arrives: `#` headings in the brand
accent, `>` quotes with a dimmed bar, `---` rules, tinted list markers,
aligned tables, fenced code blocks streamed in a uniform code color, and
inline `**bold**` / `*italic*` / `~~strike~~` / `code` / `[text](url)`
links. Backticked file names and branch names stand out in the accent
color. Reasoning-model `<think>…</think>` blocks render as a recessive
gray-bar, dim-italic region with the tags hidden, so chain-of-thought
stays visible but never competes with the reply. The render is
write-once (no repaint); `NO_COLOR` or a non-color
terminal downgrades the stream to plain text. In the
multiline input, `Home`/`End` and `Ctrl+A`/`Ctrl+E` move to the current line's
start/end; macOS `Cmd+Left`/`Cmd+Right` work when the terminal maps them to
`Home`/`End`. Set `AGENTOS_CHAT_FULLSCREEN=0` to opt out to native scrollback;
non-TTY contexts fall back automatically.

## CLI map

Top-level: `init`, `onboard`, `configure`, `doctor`, `upgrade`, `chat`,
`agent`, `reset`, plus these groups (each supports `--help`):

`agentos --version` prints the installed version and exits — use it rather
than reading a version out of `uv tool list` or `pip show`.

| Group | Subcommands |
| --- | --- |
| `gateway` | `run`, `start`, `status`, `stop`, `restart` (`--port`, `--bind`, `--listen`, `--config`, `--json`, `--debug`; `--bind-mode loopback\|lan\|tailnet\|custom` on `run`/`start`/`status`/`stop`/`restart`; `restart --force` cancels active turns; `status --deep` adds the installed service), `install` (`--port`, `--config`, `--no-start`, `--json`; macOS LaunchAgent / Linux systemd user unit that restarts the gateway; not on Windows), `uninstall` (`--json`). `restart` restarts a gateway it did not start (desktop app, launchd, systemd, terminal) in place over the `gateway.restart` RPC. `run` exits 78 on a config-class failure (bad TOML/config, failed migration, public bind without auth) |
| `config` | `get [key]` (empty key = show all), `set <dot.key> <value>` |
| `env` | `list [--missing] [--category]`, `get <NAME> [--reveal] [-y]`, `set <NAME> --stdin`, `import <NAME>`, `unset <NAME> [-y]` |
| `providers` | `list`, `status`, `configure <id> [-m MODEL] [-k API_KEY] [--base-url] [--proxy]` |
| `models` | `list [--provider] [--capability/-c] [--json]` |
| `skills` | `init`, `list`, `search`, `view`, `install`, `uninstall`, `update`, `publish`, `tap add/list/remove` |
| `sessions` | `list`, `show`, `rename`, `resume`, `abort`, `delete` (`-y`), `export` (`--format md\|json`, `--output`/`-o`; default file `<session-id>.<format>`) |
| `acp` | `list` (`--local`, `--json`; every harness id, launch source `override`/`installed`/`package-exec`, missing CLIs, allowed), `doctor [ID]` (`--probe` spawns the harness and runs ACP initialize, 30 s cap; `--local`; exits 1 when ID cannot launch), `run ID "PROMPT"` (`--cwd`, `--mode <harness session mode>`, `--oneshot`, `--resume <acp-session-id>`, `--model`, `--timeout`, `--permissions approve-reads\|approve-all\|deny-all` (default approve-reads: reads allowed, everything else rejected), `--verbose`, `--json`; local, no gateway; reply on stdout, `Stats:` line on stderr; exit 1 on error/refusal, 130 cancelled), `sessions` (`--local`, `--limit`, `--json`; gateway, else `<state>/acp/sessions/`), `cancel KEY`, `close KEY [--discard]` (gateway only). `list`/`doctor`/`sessions` ask the gateway (`acp.agents`/`acp.doctor`/`acp.sessions`) and fall back to local data |
| `projects` | `list` (`--agent`, `--json`), `create` (`--knowledge`/`--knowledge-file`, `--agent`, `--json`), `show`, `update`, `delete` (`-y`, `--json`), `move <session> <project\|none>` — group sessions across agents; the knowledge text is injected into every member session's prompt |
| `wallet` | `status`, `setup [--mode auto\|manual]`, `unlock`, `lock`, `list`, `create --label L`, `import --label L (--private-key-stdin \| --keystore FILE)`, `export ADDR (--keystore \| --private-key) [--out FILE]`, `rename ADDR LABEL`, `remove ADDR [--yes\|-y]`, `primary ADDR`, `balances [ADDR] [--chain base\|robinhood] [--refresh] [--hidden]` (the ledger's last sync; `--refresh` re-reads the chain first, throttled to once per 10 s per wallet; a `chains[].status` of `partial`/`failed` means the amounts on that chain are last-good, not fresh; junk airdrops — unlisted, no pool ≥ $1k, never traded by the wallet — are hidden and not counted, `hiddenCount` says how many, `--hidden` lists them) — the engine's wallet vault (`~/.agentos/wallets/`, a sensitive path the agent cannot read); passwords come from a hidden prompt or `AGENTOS_WALLET_PASSWORD` (`AGENTOS_KEYSTORE_PASSWORD` for an imported keystore), never argv. Everything but `status`, `list` and `balances` is operator-only: from an agent's connection it fails with `trading.operator_required` |
| `trade` | `status` (provider, key, vault, limits, chains; with `--json` also `ledgerRepair` — "full sync required" after a ledger repair migration: run `agentos trade sync --full` once, operator-only), `provider [aggregator\|uniswap]` (show/switch the swap provider: the AgentOS Aggregator is the default and needs no key, Uniswap needs `trading.uniswap_api_key`), `probe [--provider aggregator\|uniswap] [--api-key K]` (`--json` exits 1 when not ok; `--api-key` is operator-only), `tokens --chain C QUERY`, `quote --chain C --in T --out T (--amount A \| --usd D) [--wallet ADDR] [--slippage P]` (sends the initiator, so `guard.decision` is meaningful; result carries `expiresAt`), `swap --chain C --in T --out T (--amount A \| --pct P \| --usd D) [--wallet ADDR …\|--all-wallets] [--slippage P] [--note N] [--client-id ID] [--wait] [--wait-seconds 1..900] [--as-agent]`, `send --chain C --token T (--to ADDR[=AMOUNT] … \| --file F) [--amount A \| --usd D] [--wallet ADDR] [--note N] [--client-id ID] [--wait] [--wait-seconds 1..900] [--as-agent]` (several `--to` = one multisend batch with a shared `batchId`, judged and approved as one; an agent's send always waits for the user's approval; `--client-id` is an idempotency key — the same id returns the same order instead of trading twice, so reuse it on a retry), `allowances [--chain C] [--wallet ADDR] [--full] [--wait/--no-wait] [--wait-seconds 1..3600]` (live ERC-20 allowances with spender labels and `exposureUsd`; `--full` rescans from the wallet's start, `--wait` polls until the scan has caught up), `revoke --chain C --token T --spender ADDR [--wallet ADDR] [--note N] [--wait] [--wait-seconds 1..900]` (`approve(spender, 0)`; from an agent it waits for approval), `decode --chain C (TXHASH \| --data 0x… [--to ADDR])` (what a tx called and what moved), `network [--fresh]` (head block, block age, gas, RPC latency per chain), `orders [--status S] [--wallet ADDR] [--kind swap\|send\|revoke] [--limit N]`, `order ID [--wait] [--wait-seconds 1..900]`, `approve ID`, `reject ID [--reason R]`, `history [--wallet ADDR] [--chain C] [--kind swap\|deposit\|withdraw\|gas\|approval] [--limit N] [--hidden]`, `portfolio [--wallet ADDR] [--hidden]` (junk tokens are left out and never counted; `hiddenCount` says how many, `--hidden` lists them flagged `hidden`), `hide --chain C ADDR` / `unhide --chain C ADDR` (operator-only; the user's choice is final — quoting or swapping a hidden token also shows it again), `sync [--wallet ADDR] [--full]` (`--full` is operator-only), `limits [ADDR]` (default: primary) — aggregator or Uniswap swaps on Base/Robinhood Chain, orders, ledger and PnL. The aggregator (0x) refuses the 29 tokenised stocks on Robinhood Chain (AAPL, TSLA, SPY, …), but any pair with a Stock Token is routed through Uniswap automatically when a Uniswap API key is configured (`agentos config set trading.uniswap_api_key <key>`, or Settings › Trading); without a key the `trading.token_not_tradeable` error carries that hint. `markets <token> [--chain base\|robinhood] [--side all\|quote\|base] [--min-tvl USD] [--limit N] [--lookalikes] [--deep]` lists every pool a token trades in, on every DEX (default chain robinhood: `agentos trade markets NVDA --chain robinhood --json`). The gateway decides which connections are an agent's (agent token — a `cron --script` job carries one too — / exec window / nothing presented; the operator proves themself with a secret: the desktop's at spawn, or `~/.agentos/wallets/operator.secret`, rewritten by the gateway at every boot and read by the CLI on its own when `AGENTOS_AGENT_TOKEN` is unset and the gateway is local — a remote gateway gets the agent's rules): agent swaps obey `trading.approval_threshold_usd`, `trading.daily_cap_usd` (0 = agent swaps off; in-flight orders count), `trading.agent_max_price_impact_pct` (above → approval) and `trading.agent_max_slippage_pct` (above → `trading.slippage_too_high`); `approve`/`reject`, `hide`/`unhide`, `probe --api-key` and `sync --full` fail for an agent with `trading.operator_required`, `wallet status`/`trade status` omit `vaultPath` for an agent, and `config set trading.*` is refused for an agent too (rejected as an invalid request; only the operator can change those keys). Swap targets and approval spenders are pinned to the provider's known contracts (any other address in a quote is refused); the gas limit is the provider's or the estimate + 20 %, and an order the wallet cannot fund (value + gas at the quoted fee) is refused before signing. `--note` is stored sanitised (control/bidi characters dropped, whitespace collapsed, 240 chars). Other `trading.*` keys: `enabled`, `provider`, `aggregator_base_url`, `uniswap_api_key`, `uniswap_api_key_env`, `rpc_urls`, `approval_ttl_seconds`, `default_slippage_pct`, `unlock_mode`, `sync_interval_seconds`, `price_ttl_seconds`. With `--json`, argument errors are `{"error":{"code":"INVALID_ARGUMENT",…}}` on stderr, exit 2 (see the `wallet-trading` skill) |
| `cron` | `list`, `status`, `add` (also takes `--session-key`, the chat a job reports into), `update` (both take `--job-kind`, `--script`, `--script-arg`, `--workdir`, `--elevated`, `--elevated-mode`, `--tool-policy`, `--concurrency forbid\|replace\|allow`; the policy's `profile` must be one of `coding`/`full`/`memory_only`/`messaging`/`minimal`, or be omitted), `remove`, `run`, `runs`. `list --json` rows carry `concurrency` and `health` (`consecutive_failures`, `last_error`, `degraded` = 3+ failures in a row) |
| `channels` | `list`, `status`, `types`, `describe`, `native-commands`, `add`, `remove`, `enable`, `disable`, `edit`, `start [NAME] [--yes]` (no name: start every stopped channel and leave safe mode), `restart`, `logout`, `pairing …`. `status` shows each channel's `state` (`connected`/`disconnected`/`stale-socket`/`stuck`/`retrying`/`start_failed`/`stopped`/`terminal`/`blocked`) and whether safe mode is on; `--json` rows add `last_event_at`, `terminal_reason`, `start_error`, and `health_policy` holds the auto-restart policy and the last sweep/thaw |
| `memory` | `status` (`--deep`, `--agent`, `--json`), `index` (`--force`, `--agent`, `--json`), `list`, `search`, `show`, `ingest`, `curated`, `embedding-download`, `raw-fallbacks …` |
| `sandbox` | `status`, `on`, `bypass`, `full`, `reset` |
| `search` | `list`, `status [provider]`, `query`, `configure` |
| `auth` | `login xai` (`--no-wait`/`--resume`/`--json` for non-blocking use), `status`, `logout xai` — xAI OAuth (SuperGrok / X Premium+) for `x_search`; tokens in `~/.agentos/auth.json`, never printed |
| `configure x-search` | xAI X (Twitter) search: `--api-key-env`, `--x-search-model`, `--x-search-reasoning-effort`, `--no-x-search-enabled`; catalog via `onboard catalog x-search` |
| `cost` | usage and estimated cost report; `savings` for the Pilot Router savings report (`--pdf`) |
| `diagnostics` | `status`, `on`, `off` |
| `migrate` | `openclaw`, `hermes` (`--source`, `--profile` (hermes only: profile under `~/.hermes/profiles`), `--preset user-data\|full`, `--apply`, `--migrate-secrets`, `--overwrite`, `--include`, `--exclude`, `--skill-conflict skip\|overwrite\|rename`, `--persona-conflict` (openclaw only), `--json`; dry-run without `--apply`); bare `migrate` auto-detects `~/.openclaw` / `~/.hermes` |
| `agents` | `list`, `add` (`--model`, `--description`, `--workspace`, `--name`, `--json`), `delete` (`--force`/`-f`, `--json`) |
| `mcp-server` | `run` (MCP bridge) |
| `replay` | `--session`/`-s`, `--turn`/`-t` — print a recorded turn; no tools are re-executed |
| `dist` | emit workspace-state.json (`--output`/`-o` writes a file instead of stdout) |
| `reset` | `--key <session-key>` — rotate a session to a fresh transcript |
| `onboard` | first-run setup / `status` |

Built-in channel types are `discord`, `email`, `slack`, and `telegram`; use
`agentos channels types` as the authoritative catalog. Config migration backs up the
file before removing entries for retired built-in channel types.

Slack webhook entries use `webhook_path`. Omitted or empty means automatic: the
first enabled webhook account in config order keeps `/slack/events` no matter
how many other webhook accounts are enabled, so adding a second account never
changes an already-configured account's Request URL. Every other enabled
webhook account with no explicit path gets `/slack/events/<account_name>`.
Disabled and Socket Mode entries do not count, or count as "first". Non-empty
paths supplied with `channels add slack --field webhook_path=…` take
precedence. Automatic names (for every account but the first) must use
letters, digits, `.`, `_`, `~`, or `-` and cannot be `.` or `..`; use an
explicit path for other names. Configure each Slack app's Events API,
Interactivity, and slash-command Request URLs to match, then restart the gateway.
Duplicate webhook paths with overlapping HTTP methods cause a startup error.

Telegram direct messages always require pairing. Use `agentos channels pairing
list <name>`, `approve <name> <code>`, `deny <name> <sender-id>`, `revoke
<name> <sender-id>`, or `clear-pending <name>`. Pairing is binary and has no
admin/owner tier. Telegram groups are disabled by default; enable them only
with explicit `group_chat_ids`, paired senders, and the desired mention
requirement.

The `email` channel is IMAP polling in, SMTP out, and needs no platform app
registration. `allowed_senders` is a required fail-closed From-address
allowlist (exact addresses or `*@domain` patterns); one mail thread is one
session.

Platform-native interactive tool approvals (Slack block actions, Telegram inline keyboard callbacks, and Discord message components) allow operators to approve or deny gated tool executions directly using interactive buttons. For security, these approvals are restricted to channel DMs (never group chats), access-gated by evaluating the clicker's sender ID against the channel's access policy, and strictly session-bound to their originating chat context.

## Configuration

File resolution (highest precedence first):

1. Environment variables
2. `./agentos.toml` (current directory)
3. `~/.agentos/config.toml` (user-global; `AGENTOS_STATE_DIR` moves `~/.agentos`)
4. Built-in defaults

Most commands accept `--config <path>` to target a specific file. On load,
AgentOS auto-migrates outdated config schemas and writes a backup next to
the file before rewriting it.

Environment variables live in `~/.agentos/.env` and are managed with `agentos
env`, not `agentos config`. Use them for credentials skills and external
binaries read. `agentos env list` shows every variable AgentOS knows about,
whether it is set, and which skill or provider needs it — values are masked
unless you ask for `agentos env get <NAME> --reveal`. Names that steer
subprocess execution (`PATH`, `LD_PRELOAD`, `EDITOR`, …) or runtime posture
(`AGENTOS_AGENT_PERMISSIONS`, `AGENTOS_GATEWAY_TOKEN`, …) or outbound routing
(`HTTP_PROXY`, `AGENTOS_LLM_PROXY`, `AGENTOS_TRUST_ENV`, … — proxy names in any
casing) cannot be written through AgentOS; edit
the file by hand if one is genuinely needed. When
`agentos env list` reports a variable's source as `process env`, the shell
that started the gateway exported it and that value wins over the file.

Shell commands inherit most of that environment, so `gh`, `aws` and friends
work as they do in your own shell. The gateway token and the sandbox guard
switches are withheld; `AGENTOS_STRIP_PROVIDER_ENV=1` withholds AgentOS's
provider keys too. `execute_code` is the opposite — it forwards a small fixed
allowlist, and a skill reaches its own key there by declaring
`metadata.agentos.requires.env: [NAME]`, which is added for the session that
loaded the skill. Prefer that, or `$NAME` in a shell command, over pasting a
key into a payload: outbound tools refuse credential *material* (private keys,
`sk-ant-…`/`ghp_…`/`AKIA…` provider keys, DSN passwords), and command output is
masked before it reaches you. An opaque API key in an `Authorization` or
`x-api-key` header is fine and is not refused.

Main `agentos.toml` sections (full commented reference:
`agentos.toml.example` in the repo):

| Section | Controls |
| --- | --- |
| top-level | `workspace_dir`, `state_dir`, logging, `search_provider`/`search_api_key`, timeouts, and the in-memory registry ceilings `registry_session_max_entries` (default 512), `registry_cache_max_entries` (default 512), `registry_cache_ttl_seconds` (default 900) — how much per-session state one long-lived gateway process retains |
| `[x_search]` | xAI-backed X (Twitter) search: `enabled`, `model` (default `grok-4.5`), `api_key`/`api_key_env` (`XAI_API_KEY`), `reasoning_effort`, `timeout_seconds`, `total_timeout_seconds`, `retries`. The `x_search` tool is hidden until a credential resolves |
| `[browser]` | Browser automation via `agent-browser`: `enabled`, `headless`, `cdp_port` (0=managed; >0 attaches to your Chrome, localhost only) + `attach_confirmed`, `allowed_domains`, `persist_profile`, `dialog_policy`, `restrict_evaluate`. The `browser` tool is hidden until the binary is installed (`npm install -g agent-browser && agent-browser install`); it is found on `PATH` or in the usual npm global dirs (nvm, fnm, Volta, Homebrew…), else set `binary_path`. See docs/features/browser.md |
| `[llm]` | `provider`, `model`, `api_key`, `base_url`, `proxy`, `[llm.provider_routing]`, `[llm.repetition_guard]` (`enabled` default `true`; cuts a response that loops on the same line/paragraph/phrase after its first copy, continues once, then ends the turn with `repetition_detected`; thresholds `first_check_chars` 16000, `tail_chars` 64000, `window_chars` 60, `min_repeats` 5, `min_coverage` 0.5) |
| `[agentos_router]` | router on/off, `strategy` (`pilot-v1` \| `llm_judge` \| `jev`), tier settings under `[agentos_router.tiers.c0..c3]`; `jev` (experimental) reads its key from `TYPESAFE_API_KEY` and is tuned under `[agentos_router.jev]`; `effort_by_tier` (default `true`) lowers the shipped tier thinking level on cheap tiers (c0 at most `low`, c1 at most `medium`) without changing the model; router-control pins and a tier `thinking_level` set to something other than the shipped value are kept — `false` keeps each tier's level |
| `[skills]` | skill filtering/injection: `filter_strategy`, `filter_top_k`, `injection_mode`, `max_skills_prompt_chars` (default 28000), `max_skill_view_chars` (default 10000, 0 disables); `[skills.install_policy]` (`enabled`, `command`, `args`, `pass_env`, `timeout_seconds`) runs an operator's scanner on every hub install |
| `[tools]` | model-visible tools and policy; `[tools.invoke] allow` lifts named tools for `POST /api/v1/tools/invoke`, which otherwise runs only tools declared read-only and never shell, file/memory writes, spawning/sending, cron, gateway, browser, paid media or trading (404 there); file-only (config RPCs cannot add an entry); `enabled = false` runs providers in plain-text mode; `profile` (`full` \| `coding` \| `messaging` \| `memory_only` \| `minimal`) sets the base allowlist — `agentos context` prices each one; `defer_loading` (default `true`) leaves the long tail of tools (audio, projects, skill authoring, `browser`, `x_search`, `http_request`…) out of each request until the model loads them with `tool_search` — set `false` to send every tool; `parallel_calls` (default `true`) asks for several independent tool calls in one response (`parallel_tool_calls: true`) on providers known to accept the field (api.openai.com and OpenRouter) — `false` sends nothing, as before; `loop_guard` (default `enforce`) warns the model when a tool call keeps returning the same result (or two calls alternate), then answers the repeat without running it, and ends the turn with `loop_detected` on a second repeat — `observe` only logs; it resets on every new message |
| `[memory]` | memory source and embedding model, `[memory.nudge]` (periodic memory review); `recall_timeout_seconds` (default 1.5) is the hard deadline for an external `[memory.provider]` recall: a late recall does not hold the turn, it finishes in the background and stands in on the next turn if that one is late too |
| `[acp]` | external coding harnesses over ACP: `enabled` (default true), `default_agent` (`claude`), `allowed_agents` (`["*"]`), `permission_mode` (`ask` approval queue \| `approve-reads` \| `approve-all` \| `deny-all`), `non_interactive_permissions` (`fail` \| `deny`), `approval_timeout_s` (900), `turn_timeout_s` (0 = none), `max_concurrent` (8), `state_dir` (default `<state>/acp`), `isolate_codex_home` (true), `install_missing_adapters` (true; allows the `npx -y`/`uvx` adapter download), `stream_delivery` (`live` default, text streams into the child session \| `final_only`), `[acp.agents.<id>]` `command`/`args`/`env` (launch override or a new harness id). Restart the gateway after changes. See docs/acp-agents.md |
| `[sandbox]` | `sandbox`, `default_level` (DISABLED/STANDARD/STRICT/LOCKED), `backend`, network/mounts |
| `[permissions]` | `default_mode` = `off` \| `on` \| `bypass` \| `full` (pair with `agentos sandbox …`); `cron_default_mode` = `off` \| `bypass` \| `full` (default `bypass`) |
| `[scheduler]` | `startup_agent_delay_seconds` (default 120; 0 = none): agent jobs missed while the gateway was down wait this long after boot; reminders and script jobs catch up at once (at most 5, staggered over 10 s). Restart the gateway after changes |
| `[auth]` | gateway admission: `mode` (`none` on loopback, `token`, or `trusted-proxy` behind an authenticating reverse proxy), `token`, `trusted_proxy` (proxy IPs or CIDR blocks; `X-Forwarded-For` is read only from them, right to left); `[auth.rate_limit]` throttles wrong tokens per client address, whatever its `Origin` (`enabled`, `max_failures` 10, `window_seconds` 60, `lockout_seconds` 300, `exempt_loopback` true — loopback is only slowed down, except browser attempts, which have their own lockable budget): a locked-out client gets HTTP 429 + `Retry-After`, or `UNAUTHORIZED` with `details.code = "AUTH_RATE_LIMITED"` on the WebSocket |
| `[gateway]` | `bind` (`loopback` \| `lan` = 0.0.0.0, token required \| `tailnet` = `tailscale ip -4` address + 127.0.0.1, never a wildcard \| `custom` = `host`; unset = `host` decides), `public_origin` (the https origin a public console is opened at; required before the Control UI starts on a public address), `config_reload` (default true: edits to the active config file apply live, restart-only keys are reported as pending) |
| `[control_ui]` | `allowed_origins` for reverse-proxy setups (joins the origin allowlist with `cors.allowed_origins` and `[gateway] public_origin`; every bind refuses other browser origins except a private same-origin address or the gateway's own localhost); `show_thinking` (default true) streams model reasoning to the WebUI as collapsible blocks — WebUI-only, channels never receive it |
| `[updates]` | `notify` (default true) — the once-per-24h "new release available" notice |
| `[artifacts]` | `legacy_session_key_urls` (default true): the deprecated `?sessionKey=` artifact/attachment URLs keep working this release; clients mint ticketed URLs with the `artifacts.download` RPC (5-minute, connection-bound `/api/v1/artifacts/download/<conn>/<ticket>`, no auth header) |
| `[push]` | `web_enabled` (default true) — Web Push for approval prompts (the console's Settings toggle; VAPID keys in `<state_dir>/web_push_vapid.json`), `vapid_subject` |
| `[hooks]` | inbound webhooks `POST /api/v1/hooks/agent\|wake\|<name>`: `token` (own bearer, never the gateway token; unset = routes 404), `rate_limit_per_minute` (60), `max_sessions_per_hook` (1 per rolling hour), `[hooks.routes.<name>]` `agent`/`session_key`/`template` (`{{ payload.a.b }}`); `[hooks.lifecycle]` `startup`/`shutdown`/`pre_restart` shell commands (`/bin/sh`, not run on Windows; budgets 10/5/10 s). `token` and `lifecycle` are file-only (config RPCs refuse to set them) |
| `[acp]` | ACP coding harnesses spawned with `sessions_spawn(runtime="acp")`: `allowed_cwds` (default `[]`) lists extra directories a harness may use as `cwd` besides the agent workspace and managed worktrees; the workspace suits read-only work in a read-only `harness_mode`, a harness that writes needs its project listed (`agentos config set acp.allowed_cwds '["~/Code/my-app"]'` replaces the whole list) or `worktree: true`. A finished `oneshot` child still takes `sessions_send` follow-ups (its harness session is resumed); only `acp close` / `subagents kill` end it. Gateway RPC: `acp.sessions` (live harnesses with `activity`, `activity_kind`, `turn_tool_calls`), `acp.status {session_key}` (any ACP session: `idle`, `closed`, `last_turn`, ...), `acp.cancel`, `acp.close` |
| `[channels]` | messaging channels (`[[channels.channels]]` entries) |
| `[auxiliary]` | model for work AgentOS runs itself, not the agent's turn (document analysis, image description): `provider`, `model`, `timeout_seconds`, `[auxiliary.tasks.<task>]`. Empty = reuse `[llm]` |
| `[sessions]` | `auto_title` (name a new session from its first message) and `suggest_next` (after each turn the chat input shows the likely next prompt as grey text; Tab takes it, Enter sends it), each with a `*_timeout_seconds`; both default on and run on the `[auxiliary]` model (`session_title` / `suggest_next` tasks) |
| `[prompt]` | prompt-layer flags: `platform_hint_enabled`, `env_probe_enabled` (local-toolchain block, names only) |
| `[observability]` | Prometheus metrics (`metrics_enabled`, `metrics_path`), OTLP trace export (`otlp_enabled`, `otlp_endpoint`, `otlp_headers`, `otlp_service_name`), log retention (`log_retention_days`, `log_retention_max_total_mb`, `log_retention_sweep_interval_s`) |
| `[prompt_cache]` | Prompt-cache continuity: `mode` = `auto` (default) \| `on` \| `off`; `ttl` = `5m` (default) \| `1h` — how long Anthropic keeps the cached system prompt and tools (1h writes cost 2x input instead of 1.25x; worth it when a session idles past 5 min). Env overrides: `AGENTOS_CACHE_MODE`, `AGENTOS_CACHE_TTL` (legacy `prompt_cache.enabled` / `AGENTOS_CACHE_ENABLED` deprecated) |
| `[safety]` | Prompt-ingress safety: `wrap_untrusted_workspace` (default true), `injection_scan_mode` (`report` default, `enforce` redacts matched workspace-file content, `off`) |
| `[budgets]` | money spend ceilings (USD): `session_limit`/`session_warn`, `daily_limit`/`daily_warn` (per UTC day, persisted across restarts), and per-key `[budgets.agent_daily_limit]` / `[budgets.channel_daily_limit]` (plus `*_warn`). A turn at or above a hard limit is refused with `budget_exceeded`; a warn threshold raises a one-shot `budget_warning`. Each admitted turn holds `turn_reservation` (default `0.25`, `0` to disable) until it ends so a concurrent fan-out cannot clear one ceiling several times over. Nothing is enforced until a ceiling is set |
| `[compaction]`, `[agent_token_saving]`, `[task_runtime]` | context compaction, tool-result projection, concurrency. `[compaction] history_tool_result_max_chars` caps a finished turn's tool results / tool-call arguments for later turns (head/tail + store handle; `0` default = 2% of the context window, clamped 4k-12k chars); `[compaction] summary_timeout_seconds` (default 45) bounds the in-turn summary, which runs on the auxiliary `compaction` task (cheapest text tier unless `[auxiliary.tasks.compaction]` says otherwise); `[compaction] proactive` (default true) compacts a session in the background between turns once its history reaches `[compaction] proactive_ratio` (default 0.70) of the trigger, after the session has been idle `[compaction] proactive_idle_seconds` (default 20; doubled per session up to 300 after a turn cancels a running summary; `0` = at once); `[compaction] estimate_calibration` (default true) scales the trigger's size estimate by the input tokens providers report; `[task_runtime] concurrent_turn_setup` (default true) runs the independent reads before a turn's first model request concurrently, `false` restores the serial order |

Slack native commands auto-sync when a Slack channel entry provides `app_id`,
`manifest_token` (an app configuration access token), and `command_request_url`;
otherwise export them with `agentos channels native-commands slack --request-url …`.

## Common operations (verified recipes)

### Change the model/provider (persistently)

```sh
agentos providers configure openrouter -m anthropic/claude-sonnet-4   # provider + model in one step
agentos config set llm.model "anthropic/claude-sonnet-4"              # just the model key
agentos configure                                                     # interactive wizard
agentos gateway restart                                               # apply to a running gateway
```

### Enable X (Twitter) search

`x_search` searches X posts through xAI and returns a synthesized answer with
citations. It is **hidden from your own tool list until a credential resolves**,
so if you cannot see it, that is why — say so and offer one of these:

```sh
agentos auth status                       # is a SuperGrok / X Premium+ login stored?
agentos configure x-search --api-key-env XAI_API_KEY   # or use an xAI API key
```

A subscription login is preferred over a key when both exist. The Web UI has
the same controls under Capabilities. Billing goes to the user's xAI account
and does not appear in `agentos cost`.

### Sign the user in to xAI (SuperGrok / X Premium+) from a conversation

Enables `x_search` without an API key. Never run the plain `agentos auth login
xai`: it blocks polling for up to 30 minutes and will hit the tool timeout
before the user can approve. Use the split form.

```sh
agentos auth login xai --no-wait --json 2>/dev/null   # -> loginId, verificationUri, userCode
```

**Keep the `2>/dev/null`.** Every `agentos` invocation writes startup logs to
stderr, and merged into the result they bury the one line that matters — the
JSON. With stderr dropped the output is a single object you can read reliably.

Give the user the `verificationUri` and the `userCode`, then **wait for them to
say they have approved it**. Do not poll in a loop — approval is human-paced,
and each check costs a turn.

```sh
agentos auth login xai --resume --json 2>/dev/null    # exit 0 done, 3 not yet, 1 failed/expired
agentos auth status --json 2>/dev/null                # confirm; never prints a token
```

Exit 3 means keep waiting, not an error. On exit 1 the code expired — start
over. The user code and link are safe to show in chat; neither works without
the user's own xAI session, and no token ever reaches the conversation.

### Gateway lifecycle

```sh
agentos gateway start          # background; `run` = foreground
agentos gateway start --port 9000   # `run`/`start` share --port/--bind/--listen
agentos gateway status --json  # machine-readable status
agentos gateway stop
```

Default port **18791**, loopback bind. `--listen HOST` overrides
`--bind` (use `--port PORT` to specify port, e.g. `--listen 0.0.0.0 --port 18791`).
`--bind-mode lan|tailnet|loopback|custom` (or `[gateway] bind`) picks a listener
without naming an address; `tailnet` fails rather than fall back to `0.0.0.0`.
`status`/`stop`/`restart` follow the mode `start` recorded.
`gateway status` (and `--json`) reports **both**
the installed CLI version (`cliVersion`) and the running gateway's version
(`gatewayVersion`); a `versionMismatch` diagnostic means the gateway is running
old code — restart it.

`gateway status --json` also carries `drain_budget_ms`, `active_turns` and
`runtime.state` (`starting` / `ready` / `draining` / `safe_mode`, with
`runtime.failing`); the top-level `state` is the process state. The same
readiness is `GET /ready` (`503` while starting or draining); `GET /startupz`
is `200` once channels and the router preload settled.

To keep the gateway running across crashes and reboots:

```sh
agentos gateway install --port 18791   # macOS LaunchAgent / Linux systemd user unit
agentos gateway status --deep          # service installed? loaded?
agentos gateway uninstall
```

`install` refuses when a different unit with the same name exists (uninstall
first) and is a no-op for the same unit. A gateway that crashed three times in
five minutes boots in **safe mode**: the control plane works, but channels,
cron and heartbeat are held back; `/ready` says `state: "safe_mode"` and
`agentos doctor` shows a `gateway.safe_mode` finding. It clears by itself
after five quiet minutes; `agentos channels start --yes` leaves it at once
(`agentos channels start NAME --yes` starts one channel to find the bad one).
After a laptop sleep the gateway restarts idle channels itself (a restart
that fails is retried with backoff), and a 5-minute health sweep restarts
`disconnected`, `stuck`, `start_failed` and `stale-socket` channels — never
`terminal` (bad credentials) or `blocked` ones. `gateway stop` does not stop
a service-run gateway: it names the `launchctl bootout` / `systemctl --user
stop` command or `gateway uninstall`.

`agentos gateway restart` restarts a gateway started by `gateway start`
(stop, then start) and restarts any other healthy gateway (desktop app,
launchd, systemd, a terminal `gateway run`) **in place**: the gateway drains
its active turns and re-executes itself under the same pid, so every launcher
keeps tracking it. `--force` cancels the active turns instead. Not on Windows.
The same restart is `/restart [force]` in the web console and the desktop app
(operators, always), and from a paired channel or the agent's `gateway` tool
only when `[gateway_control] chat_restart = true` (default `false`; takes
effect after one restart). The gateway posts "Gateway restarted" back into
the chat that asked once it is up again.

### Version

`agentos --version` (or `-V`) prints the installed version and exits; it loads
no config and probes nothing, so it is the cheap "is the engine there?" check
(the macOS app runs it at launch). `install.sh --manifest` / `--stage NAME
--json` expose the installer's stages for that app's first-run install.

### Upgrading AgentOS

```sh
agentos upgrade                # snapshot, upgrade, then restart + verify the gateway
agentos upgrade --check        # is a newer release available? changes nothing
agentos upgrade --dry-run      # print the command that would run; touch nothing
agentos upgrade --no-restart   # upgrade only; gateway keeps running OLD code
agentos upgrade --source github            # install the GitHub release wheel
agentos upgrade --verify-data              # quick_check every state database
agentos upgrade --restore-snapshot latest  # put the last snapshot back (gateway stopped)
```

`agentos upgrade` is the primary path: it detects the install method and
installs the **published release** of `use-agent-os[recommended]` (`uv tool
install --force --python <running> …` / `pipx install --force …`) — from PyPI,
or with `--source auto` (default) from the GitHub release wheel when GitHub is
ahead or PyPI is unreachable — then by default restarts the managed gateway and
verifies it reports the new version before declaring success. It never installs
from a local checkout — even when the current install came from one — because
only `bash scripts/install_source.sh` rebuilds the React control UI before
installing, and a release wheel already ships a CI-built one. A checkout-backed
install gets an informational note naming that directory and the script; it
never blocks. For pip / editable / portable / unknown installs it prints the
exact manual command and exits non-zero (**exit 3**) rather than faking it. A
portable install (the Windows zip's venv, built without pip) prints `python -m
ensurepip`, then `python -m pip install --upgrade "use-agent-os[recommended]"`,
as two lines to run in order. A failed, unverifiable or data-check-failed
upgrade is **exit 1**. Flags: `--timeout`
(subprocess bound, default 600s; kills the process group on timeout),
`--no-snapshot`, `--config`, `--json` (adds `source`, `snapshot`, `data`,
`sourceDirectory`).

Before installing, `config.toml`, `auth.json`, `skills-lock.json` and every
SQLite file under `~/.agentos/state/` are snapshotted to
`state/snapshots/pre-upgrade-<utc>/` (newest three kept). After the restarted
gateway has run its migrations, every database gets a `PRAGMA quick_check`; on
failure the managed gateway is stopped, the snapshot restored and the gateway
started again. The Control UI banner's **Update now** runs the same command as
a detached job via the `updates.apply` / `updates.status` RPCs; the macOS app
runs it with `--no-restart` and restarts the gateway it spawned.

On Windows the managed gateway is stopped before the installer runs and started
again afterwards — Windows cannot replace files a live process holds open, and
the gateway runs the tool venv's own interpreter (that is the `Access is denied`
on `…\uv\tools\use-agent-os\Scripts`). `--no-restart` opts out and can
therefore still hit the lock. When the installer is refused anyway, the error
names the recovery: stop the gateway, close every other AgentOS process, re-run
the printed command from a fresh terminal, and restore uv's tool bin directory
to PATH with `uv tool update-shell` if `agentos` has gone missing.

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
agentos config set auth.mode token --config ~/.agentos/config.toml
agentos config set auth.token "<long random secret>" --config ~/.agentos/config.toml
agentos gateway restart
```

Without `--config`, `config set` only prints an `export` line; name the file
the gateway actually reads (`agentos doctor` shows it). Unauthenticated
non-loopback Control is unsupported. A VPN or firewall does
not replace the AgentOS Control token; the one alternative is
`auth.mode = "trusted-proxy"` with `auth.trusted_proxy` naming a reverse proxy
that authenticates users itself (only that peer is admitted). Behind a reverse
proxy on another browser origin, also set `control_ui.allowed_origins`. To
reach a gateway from another machine without opening a port, keep it on
loopback and tunnel in: `ssh -N -L 18791:127.0.0.1:18791 user@host`, or
`tailscale serve --bg --https=443 http://127.0.0.1:18791` plus the `.ts.net`
origin in `control_ui.allowed_origins` (full guide: `docs/remote-access.md`).
Behind a proxy on the same host (Tailscale Serve, Caddy) every client looks
like loopback, which is exempt from the failed-token lockout: also set
`auth.trusted_proxy` to the proxy (`127.0.0.1`) or
`auth.rate_limit.exempt_loopback = false`. `agentos doctor` reports risky
exposure (`gateway.bind_no_auth`, `gateway.allowed_origins_wildcard`,
`gateway.cors_wildcard_with_credentials`, `gateway.trusted_proxy_unset`,
`gateway.auth_rate_limit_off`,
`gateway.rate_limit_loopback_exempt_behind_proxy`, `gateway.token_short`) with a
fix for each; it presents `AGENTOS_GATEWAY_TOKEN` / `auth.token` / `--token`
and falls back to checking the config file when no gateway answers.

### Skills

```sh
agentos skills list                    # installed, per layer
agentos skills search <query>
agentos skills install <name>                    # from ClawHub (default source)
agentos skills install owner/repo:path -s github # from a GitHub repo/URL
agentos skills install <bankr-skill-url> -s bankr # from Bankr (repo or bankr.bot URL)
agentos skills install <aeon-skill-url> -s aeon   # from Aeon (aeonfun/aeon skills/<slug>)
agentos skills tap add owner/repo      # register a GitHub repo as a skill source
agentos skills tap list
agentos skills tap remove owner/repo
agentos skills update
agentos skills uninstall <name>
```

**Before concluding a skill is missing, run `skill_list`.** A skill that
declares `requires` is dropped from your prompt until its binary and variables
are present, so an installed-but-unconfigured skill and a skill that was never
installed look identical from inside a turn. `skill_list` shows both, and marks
the first `[unavailable]` with what it is missing and the command that fixes
it. Reporting that is the answer to "install X" when X is already here — an
install would not have helped.

**Never install a hub skill over a bundled one.** Layer precedence means the
managed copy wins, so the built-in stops running everywhere while its files sit
untouched on disk — nothing in the session would show the swap. The installer
refuses this and says so; only pass `force` after the operator has confirmed
they want the hub version instead of the shipped one. `skill_search_community`
answers with an `installed_match` block when the query names a skill this
machine already has, for the same reason.

**An install can be refused for security, and `force` is not a retry.** Three
checks run on a hub skill: the registry's own verdict, the built-in scan, and
the operator's `[skills.install_policy]` command when one is configured. A
refusal whose result carries `overridable: true` (registry `suspicious`, scan
`dangerous`, policy `warn`) may be forced, but only after you have shown the
operator the reason and they have said to go ahead. A registry `malicious`
verdict and a policy `block` cannot be forced; report them and stop. A policy
that is enabled but broken refuses every install — that is the operator's
setting working, so fix the `command`, do not turn the policy off.

Three separate facts describe a skill; do not use one to answer another.

**Layer — where the files are.** Name-collision precedence, later overrides
earlier: `extra` (config dirs) → `bundled` (shipped) → `managed`
(`~/.agentos/skills`, where installs land) → `personal` (`~/.agents/skills`)
→ `project` (`<workspace>/.agents/skills`) → `workspace`
(`<workspace>/skills`). Layer is also the tiebreak when the prompt budget
forces a cut: shipped skills go first, an operator's own skills go last.

The `.agents/skills` layers are shared with other agents (Codex, Cursor, and
anything else using that convention). Skills they add or remove take effect on
the next turn — the loader re-checks the directories rather than trusting a
cache — so do not tell an operator to restart the gateway to pick one up, and
do not assume `agentos skills install` is the only way a skill got there. A
`.agents/skills` skill also outranks a bundled or hub-installed one of the
same name.

**Acquisition — how it got there.** `shipped` (ships with the wheel),
`hub` (fetched by `agentos skills install`, has a lockfile entry), or `local`
(a directory someone put there). `agentos skills list --json` reports it under
`acquisition`, together with `source_id`, `author`, `identifier`, `version`,
`installed_at`, and two booleans: `removable` and `updatable`. `author` is the
credit the catalog row carried (e.g. `@igoryuzo`) — untrusted free text, not a
brand, and empty only when it would merely repeat the resolved publisher. Trust those
booleans rather than inferring from the layer — a hub install whose recorded
path no longer matches the configured `skills.managed_dir` reports
`removable: false` (AgentOS will not delete files it cannot prove it owns)
while `updatable` stays true, because an update re-fetches by identifier.

**Publisher — whose name is on it.** `agentos skills list --json` reports
`publisher` as `{id, name, url, logo}`, empty strings when unbranded.
The id is *allowlisted server-side*: a `SKILL.md` or a hub catalog can only
select from the recognized set, never describe a publisher of its own. Only a
`bundled` manifest may select an id for itself; an installed skill is branded
by the hub catalog row it came from, so a directory added by hand is always
unbranded. Treat a non-empty `publisher.id` as the only signal of a partner
skill; never infer one from a name or a homepage URL.

Provenance (`origin`, `license`, `upstream_url`) is a fourth, independent
fact — where the text came from and under what licence. A skill can be
AgentOS-original text published by a partner, or upstream text with no
publisher at all.

**Status — can it run.** `skills.list` reports `status` as `ready`,
`needs_setup`, or `not_declared`, plus a `disabled` boolean and a
`status_detail` line. `ready` and `not_declared` both mean the skill runs —
the first declared requirements and meets them, the second declared none to
check — so the Web UI counts them together as **Ready**. `needs_setup` covers
a missing binary, a missing or **blank** required env var (`export KEY=` does
not count as configured), a wrong OS, and a config-disabled skill; read
`disabled` to tell the last one apart, because it is the only one no install
will fix.

**Availability — whether the agent is being offered it right now.** This is
not the same as installed or eligible. `skills.list` over the gateway, and the
agent's own `skill_list` tool, report `availability: {offered, reason, detail}`
with `reason` one of:

| `reason` | Means |
| --- | --- |
| `""` | offered (`offered: true`) |
| `model_invocation_disabled` | the manifest sets `disable-model-invocation`; only a person can run it |
| `ineligible` | a required binary, env var, or OS is missing, or the skill is disabled in config |
| `tool_gate` | its `requires_tools` are not enabled in this session |
| `fallback_superseded` | it is a fallback for a tool the session already has natively |
| `not_retrieved` | `skills.filter_enabled` is on and this message did not match |
| `prompt_budget` | it is ready, but the skills block hit `max_skills_prompt_chars` |

All of these reach a `skills.list` row except `not_retrieved`, which ranks
against one message's wording and so only exists inside a turn. Do not tell an
operator to look for it on the Skills page — read it from the decision log.

`agentos skills list --json` omits `availability` entirely: a CLI process has
no chat session and no tool surface, so it cannot answer. An absent key means
"not computed", never "not offered".

### One-shot automation

```sh
agentos agent -m "summarize README.md" --model gpt-5.4-mini --timeout 120
agentos agent --json -m "Return a machine-readable summary"
agentos agent --workspace /path --workspace-strict -m "Inspect this repo"
```

Bounding flags: `--timeout` (wall-clock seconds), `--max-iterations`,
`--iteration-timeout-seconds`, `--tool-timeout-seconds`, `--request-timeout-seconds`;
containment: `--workspace-strict` (reads), `--workspace-lockdown` (writes),
`--scratch-dir`; execution control: `--file`/`-f`, `--unattended`/`--interactive`,
`--stateless`/`--clean-room`, `--stateless-keep-project-rules`, `--no-memory-capture`,
`--session-id`.

### Day-two operations

```sh
agentos sessions list / show <id>
agentos sessions export <id> --format md --output notes.md   # --format json|md; no --output writes <id>.md
agentos sessions delete <id> -y
# Label a session so it is findable later; --search matches the name.
agentos sessions rename <id> "api-refactor"   # --clear drops the name
agentos sessions list --search api-refactor
# In chat, /rename does the same. The `session_rename` tool names the session
# the agent is running in — use it when the user just asks in prose.
# Group related sessions into a project; its knowledge text is injected into
# every member session. Delete keeps the sessions (they just detach).
agentos projects create "Token research" --knowledge-file notes.md --agent main --json
agentos projects move <session-id> <project-id>   # 'none' detaches
agentos projects show <project-id>
agentos projects delete <project-id> -y --json
# External coding harnesses (Claude Code, Codex, Gemini CLI, ...) over ACP.
# In chat, delegate with sessions_spawn(runtime="acp", acp_agent="claude",
# task=..., cwd=..., mode="oneshot") then sessions_yield; the sub-agent skill
# has the routing table. The CLI is for checks and manual runs:
agentos acp doctor claude --probe            # CLI on PATH, adapter, ACP initialize
agentos acp run codex "List the failing tests" --cwd ~/code/app --json
agentos acp sessions                         # then: agentos acp cancel|close <full key>
# An agent stops its own harness with subagents(action="kill", session_key=...),
# never with this CLI.
agentos config set acp.permission_mode approve-reads   # reads auto-allowed, rest asks
agentos agents add research --name Research --model gpt-5.4-mini --description "research agent"
agentos agents delete research -f
# Wallet vault + Uniswap trading (Base, Robinhood Chain). The vault password is
# prompted or read from AGENTOS_WALLET_PASSWORD — never put it on the command
# line. Swaps you run inside an agent turn are agent-initiated: above
# trading.approval_threshold_usd they wait for the user in the app, and
# trading.daily_cap_usd per wallet per day is enforced by the engine.
agentos wallet setup --mode auto        # once; auto = unlock.key (0600), manual = per session
agentos wallet create --label main / list / balances [--chain base|robinhood] [--refresh]
agentos trade status --json             # provider, API key configured? vault unlocked? limits; ledgerRepair set → agentos trade sync --full once
agentos trade provider uniswap          # switch swap provider (aggregator is the default; no key needed)
agentos trade probe --provider uniswap  # reachable? key valid? (exit 1 when not ok)
agentos trade tokens --chain robinhood AAPL --json     # verified Stock Tokens are flagged
agentos trade quote --chain base --in ETH --out USDC --amount 0.01 --json
agentos trade swap --chain base --in ETH --out USDC --amount 0.01 --wait --json
agentos trade orders --status awaiting_approval [--kind swap|send|revoke|lp_collect|lp_remove|lp_add] / approve <id> / reject <id>
agentos trade send --chain base --token USDC --to <addr> --amount 25 --json   # several --to = one batch; agent sends always wait for approval
agentos trade allowances --json / revoke --chain base --token <addr> --spender <addr>   # live ERC-20 allowances; approve(spender, 0)
agentos trade decode --chain base <txhash> --json / network --json   # explain a tx; head block, gas, RPC health
agentos trade lp pool <token|TOKEN/QUOTE|poolId> [--chain base|robinhood] [--quote Q] [--fee 0.05] --json   # Uniswap V4 read: deepest pool (or the one on --fee: 0.05 / 0.05% / 500 / dynamic), reserves, TVL, launcher, LP lock; with --json also writes a chat card; no pool on the tier → trading.lp.not_found listing the tiers that exist
agentos trade lp ranges <token|TOKEN/QUOTE|poolId> [--chain C] [--quote Q] [--fee F] --json / position <tokenId> --chain C --json   # liquidity distribution; one position NFT
agentos trade lp positions [--wallet ADDR]… [--chain C]… [--all] [--budget-seconds N] --json   # every V4 position of the vault's wallets, out of range first (≤ 50 rows, totals over all); --chain repeatable (none = both); answers within 25 s (--budget-seconds 5-300), partialScan + a warning when cut short; --no-card skips the card; a token as --wallet → trading.lp.not_a_wallet
agentos trade lp collect <tokenId> --chain C [--allow-empty] / remove <tokenId> --chain C [--pct 100] [--slippage 1] [--wait] --json   # LP writes on a vault position: one order each, ALWAYS awaiting_approval (whoever asks); remove 100 burns the NFT; collect with zero fees → trading.lp.nothing_to_collect unless --allow-empty
agentos trade lp add <token|TOKEN/QUOTE|poolId> --chain C [--quote Q] [--fee F] (--usd X | --amount-base A [--amount-quote B]) [--range mcap:2M-10M|pct:20|above[:N]|below[:N]|full|ticks:LO:HI] [--to-position <tokenId>] [--wallet ADDR] [--slippage 1] [--wait] --json   # mint / top up; default range pct:20; above:N = one-sided all-token band from just above the price up N % (below:N = all-quote, down N %; N default 20); no auto-swap (short side → trading.insufficient_balance); an agent's add counts toward the daily cap; confirmed + --json → refreshed position card
agentos trade markets <token> [--chain C] [--side all|quote|base] [--min-tvl 10000] [--limit 50] [--lookalikes] [--deep] --json   # every pool the token trades in on every DEX ("pairs of NVDA", "what trades against X", "tokens priced in X"): quote section = tokens priced in it, base section = it priced in others; default chain robinhood (only trade command that defaults to it); lookalikes hidden unless --lookalikes; --json writes a markets-cards/ chat card; one pool's liquidity stays lp pool
agentos trade dca create <token> --usd X --every 30m|2h|1d|1w|<seconds> (--cap X | --runs N) [--max-price X] [--quote Q] [--chain C] [--wallet ADDR] [--slippage 1] [--name …] [--start now|next] --json   # DCA mandate the engine runs itself under a hard cap; from an agent ALWAYS awaiting_approval (user approves on the card / Missions panel); --json writes a dca-cards/ chat card
agentos trade dca list [--all] [--wallet ADDR] --json / show <id> --json   # mandates: status, spent of cap, next buy, avg vs price now
agentos trade dca approve|reject|pause|resume|stop|run|update <id> … --json   # the user's only (trading.operator_required for an agent); run = buy now [--wait]; update [--usd] [--cap] [--runs] [--every] [--max-price] [--name]; stop/reject take --reason
agentos trade trigger create <token> (--below 3800|-10% | --above 5000|+15% | --trail 10) (--sell (--pct 50 | --amount 0.05 | --usd 100) | --buy --usd 50 | --alert) [--quote Q] [--chain C] [--wallet ADDR] [--slippage 1] [--name …] [--for 7d] --json   # --quote defaults to the chain's USDC, or the native coin when USDC itself is the token; price trigger (stop-loss, take-profit, trailing stop, buy the dip, alert) the engine watches and fires itself after 2 confirming checks; never poll the price or schedule a cron for it; from an agent ALWAYS awaiting_approval (user approves on the card); --json writes a trigger-cards/ chat card
agentos trade trigger list [--all] [--wallet ADDR] --json / show <id> --json   # triggers: status, condition, price now and distance, checks, recent fires
agentos trade trigger approve|reject|pause|resume|stop|fire <id> … --json   # the user's only (trading.operator_required for an agent); fire = fire now [--wait]; stop/reject take --reason
agentos trade protect <token> --tp 4560|+20% (--sl 3420|-10% | --trail 10) [--pct 100 | --amount 0.05 | --usd 100] [--tp-pct 50] [--alert] [--quote Q] [--chain C] [--wallet ADDR] [--slippage 1] [--name …] [--for 7d] --json   # bracket: take-profit + stop-loss on ONE position, whichever fires first stops the other (OCO); default the whole position (--pct 100); --tp-pct 50 takes profit on half, the stop guards the rest; --alert = range alert, no size; a request with an exit above AND below is one bracket, never two triggers; from an agent ALWAYS awaiting_approval (one Approve & arm for both legs); --json writes a trigger-cards/bracket-… chat card
agentos trade bracket list [--all] [--wallet ADDR] --json / show <id> --json   # brackets (brk_…): status, both lines with the price now between them, upside/downside, reward:risk, each leg's checks, fires
agentos trade bracket approve|reject|pause|resume|stop|fire <id> … --json   # the user's only (trading.operator_required for an agent); act on both legs; fire [--leg tp|sl] [--wait] = fire one leg now (default the nearer); stop/reject take --reason; the legs refuse trade trigger writes and are not in trade trigger list
agentos trade portfolio / history / limits <addr>
agentos config set trading.uniswap_api_key <key>       # or Settings › Trading in the app
agentos cron list / add / run <id> / runs
agentos cron remove <id> -y --json
agentos channels edit <name> --field key=value
agentos channels logout <name> -y
agentos channels pairing clear-pending <name>
# --job-kind decides what fires. Default 'auto' = reminder: --text is delivered
# verbatim and NO LLM runs, so a job that should think needs agent_turn.
agentos cron add --every 1h --job-kind agent_turn --text "Summarize updates"
# script jobs run a file in ~/.agentos/scripts/ and deliver its stdout — no
# model, no tokens. Empty stdout = silent; non-zero exit delivers the error and
# fails the job. Relative paths only; CLI/Web callers only (never a channel).
# Subdirectories are fine, and '{job_id}' in the path becomes the new job's id,
# so a job can own a directory named after itself in a single add.
agentos cron add --every 5m --script watch-memory.sh --name memory-watchdog
# ...but a job added from the CLI has no chat to deliver into, so that stdout
# only reaches the run record. --session-key names the chat it reports into
# (the run stays isolated). Web UI / in-chat jobs already carry their session.
agentos cron add --every 5m --script watch-memory.sh --session-key "$KEY"
# 'runs' shows each run's Output + Delivery; Output is a 500-char preview.
# 'output' prints one run in full — latest by default, or --run <run-id>.
# Delivery 'fwd:no_session_target' == printed something, reached no chat.
# In chat, the cron tool reads the same history via action="runs" — use it to
# answer "what did that job do?" instead of guessing from the schedule. Editing
# a job in chat is action="update" (action="get" reads its settings, clone_from
# on add derives a second one) — never add-a-replacement + remove, which resets
# kind, tz, tool policy, and delivery to defaults.
agentos cron runs <id> [--json]
agentos cron output <id> [--run <run-id>] [--json]
# --script on an agent_turn is a pre-run collector instead: its stdout becomes
# the turn's context, and a tick that prints nothing skips the turn (no tokens).
agentos cron add --every 10m --job-kind agent_turn --script watch_rss.py \
  --script-arg --url --script-arg https://example.com/feed.xml \
  --text "Summarize anything urgent."
# Cron turns run elevated by default (cron_default_mode="bypass"): an agent-turn
# job CAN run shell-based skills unattended, with no approval prompt, no sandbox,
# host shell as the user. --no-elevated opts one job out of that, running it
# read-only instead. See docs/cli.md before suggesting either.
agentos cron add --every 6h --agent main --no-elevated --name "LP check" --text "..."
# A run still going when the next one is due: --concurrency forbid (default,
# skip it), replace (cancel the running one), allow (run both). After a laptop
# sleep an overdue job runs once, not once per missed slot.
agentos cron update <id> --concurrency replace
agentos memory status --deep --agent main
agentos memory index --force --agent main
agentos context --top 10 --json
agentos doctor --quick --json          # --deep is the default; --token T for a token-mode gateway
agentos models list --provider openrouter -c tools --json
agentos search status                  # optional provider id: agentos search status brave
agentos auth logout xai
agentos dist -o workspace-state.json
agentos reset --key <session-key>
agentos replay -s <session-key> -t <turn-id>   # print a recorded turn; read-only
agentos cost                   # usage + estimated spend
# cost support filtering and grouping:
# agentos cost [--by-model] [--json] [--csv]
# agentos cost --start-date YYYY-MM-DD --end-date YYYY-MM-DD
# agentos cost --agent-id <agent-id> --channel-type <channel-type>
# agentos cost --tool-name <tool-name> --skill <skill-name>
# agentos cost --export /path/to/export.csv
agentos cost savings           # what the Pilot Router saved, from the local decision log
# agentos cost savings [--json] [--csv] [--start-date …] [--end-date …] [--log-dir …]
# agentos cost savings --pdf /path/to/report.pdf   # branded, shareable PDF
# Baseline = the priciest model in [router.tiers], input tokens only, routing
# mechanism only. Reads ~/.agentos/logs/decisions-*.jsonl; no gateway needed.
agentos diagnostics on         # runtime diagnostics logging
agentos sandbox status         # inspect current sandbox posture
agentos sandbox on             # restore standard sandboxed posture
agentos sandbox bypass         # disable sandboxing with sensitive path protection
agentos sandbox full           # disable sandboxing and all approval gates
agentos sandbox reset          # reset posture to defaults
agentos migrate hermes --source <dir> [--profile <name>] [--preset full] [--apply]   # dry-run first, then --apply
```

## Gateway HTTP API

The gateway is also a REST + WebSocket server on port 18791:
`GET /api/config|sessions|agents|cron|usage|system/status|channels/status`,
`POST /api/chat`, `GET /api/chat/history`, approvals endpoints under
`/api/approvals*`, `POST /api/v1/rpc` (`{id?, method, params?}` → `{id, ok,
payload|error}` for an allowlist of reads and of methods that create, rename or
send (a `chat.send` there runs a turn exactly as over WS); config writes,
secrets, env, trading, restarts and deletes answer 403 `FORBIDDEN`),
`POST /api/v1/tools/invoke` (run one tool, no model turn; `dry_run` returns
the resolved policy), `POST /api/v1/hooks/*` (inbound webhooks, `[hooks]
token`), and `WS /ws` (primary RPC transport; `projects.*` CRUD lives there
alongside `sessions.*`; `artifacts.download` mints ticketed file URLs,
`push.web.*` manages approval push subscriptions, `config.changed` reports
edits of the config file the gateway applied or skipped). On loopback binds
auth is optional; on public binds the `[auth]` token gates every request
(or, in `trusted-proxy` mode, only the listed reverse proxy is admitted).
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
  `agentos models list`, then `agentos providers configure …`. The `circuit`
  column shows the failover circuit breaker: `open (42s)` means recent
  provider-health failures parked it and turns are on the fallback chain; it
  re-probes itself, so tune `[llm.circuit_breaker]` rather than restarting.
- **Skill missing from prompt** → do not guess from the layer. Ask the
  surface that knows: the `availability.reason` on a `skills.list` row, or the
  `[not offered] …` line the agent's own `skill_list` prints. Then act on the
  reason:
  - `ineligible` → the detail names the missing binary or variable;
    `agentos env set <NAME> --stdin` applies to the running gateway, no
    restart, and the skill becomes eligible on the next turn.
  - `prompt_budget` → the skills block is full. Raise
    `agentos config set skills.max_skills_prompt_chars 32000` (default 28000)
    and restart the gateway. The gateway also logs
    `skills_filter.budget_truncated` with the dropped names. Truncation goes
    lowest-precedence layer first (`extra`, then `bundled`), so this shows up
    as shipped skills disappearing while `managed`/`personal`/`project`/
    `workspace` ones survive.
  - `not_retrieved` → only possible with `skills.filter_enabled = true`
    (off by default); raise `filter_top_k` or reword the request.
  - `tool_gate` / `fallback_superseded` → about the session's tool surface,
    not the skill; check `[tools]`.
  - `model_invocation_disabled` → working as declared; the skill is for a
    person to run, not the agent.
- **Skill shows "ready" in the Web UI but the agent will not use it** →
  that is `availability`, not `eligible`. A skill can be perfectly installed
  and still be withheld for any of the reasons above; the Skills screen labels
  it on the card.
- **Deep debugging** → `agentos diagnostics on`, reproduce, then
  `agentos replay` on the recorded turn.

## Docs map

`docs/README.md` is the index; per-topic pages mirror to
`https://useagentos.dev/docs/<page>`: `quickstart`, `cli`, `configuration`,
`gateway`, `http-api`, `providers-and-models`, `channels`, `operations`,
`scheduling`, `sessions`, `usage-and-cost`, `tools-and-sandbox`,
`approvals-and-permissions`, `mcp-server`, `troubleshooting`, and
`features/skills`, `features/agentos-router`, `features/memory`,
`features/browser`, `features/compaction-and-cache`.
