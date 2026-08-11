---
name: agentos
description: "Operate and configure AgentOS itself: the `agentos` CLI, agentos.toml, gateway/Web UI, providers and models, web search, X (Twitter) search via xAI (`x_search`; SuperGrok login or XAI_API_KEY), skills, channels, sessions, cron, sandbox, and memory. Use when: (1) the user asks to change an AgentOS setting or turn a capability on (model, provider, router tier, auth/login, channels, search), (2) starting, stopping, or debugging the gateway or Web UI, (3) installing, updating, or removing skills and taps, (4) inspecting sessions, usage/cost, cron jobs, or diagnostics, (5) migrating from another agent runtime. NOT for: authoring new skills (see docs/features/skills.md), operating other agent CLIs, or modifying AgentOS source code."
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

| Group | Subcommands |
| --- | --- |
| `gateway` | `run`, `start`, `status`, `stop`, `restart` (`--port`, `--bind`, `--listen`, `--config`, `--json`, `--debug`) |
| `config` | `get [key]` (empty key = show all), `set <dot.key> <value>` |
| `env` | `list [--missing] [--category]`, `get <NAME> [--reveal]`, `set <NAME> --stdin`, `import <NAME>`, `unset <NAME>` |
| `providers` | `list`, `status`, `configure <id> [-m MODEL] [-k API_KEY] [--base-url] [--proxy]` |
| `models` | `list` |
| `skills` | `list`, `search`, `view`, `install`, `uninstall`, `update`, `publish`, `tap add/list/remove` |
| `sessions` | `list`, `show`, `resume`, `abort`, `delete`, `export` |
| `cron` | `list`, `status`, `add` (also takes `--session-key`, the chat a job reports into), `update` (both take `--job-kind`, `--script`, `--script-arg`, `--workdir`, `--elevated`, `--elevated-mode`, `--tool-policy`; the policy's `profile` must be one of `coding`/`full`/`memory_only`/`messaging`/`minimal`, or be omitted), `remove`, `run`, `runs` |
| `channels` | `list`, `status`, `types`, `describe`, `native-commands`, `add`, `remove`, `enable`, `disable`, `edit`, `restart`, `logout`, `pairing …` |
| `memory` | `status`, `index`, `list`, `search`, `show`, `embedding-download`, `raw-fallbacks …` |
| `sandbox` | `status`, `on`, `bypass`, `full`, `reset` |
| `search` | `list`, `status`, `query`, `configure` |
| `auth` | `login xai` (`--no-wait`/`--resume`/`--json` for non-blocking use), `status`, `logout xai` — xAI OAuth (SuperGrok / X Premium+) for `x_search`; tokens in `~/.agentos/auth.json`, never printed |
| `configure x-search` | xAI X (Twitter) search: `--api-key-env`, `--x-search-model`, `--x-search-reasoning-effort`, `--no-x-search-enabled`; catalog via `onboard catalog x-search` |
| `cost` | usage and estimated cost report |
| `diagnostics` | `status`, `on`, `off` |
| `migrate` | `openclaw`, `hermes` (`--source`, `--profile`, `--apply`, `--migrate-secrets`; dry-run without `--apply`) |
| `agents` | `list`, `add`, `delete` (durable agents) |
| `mcp-server` | `run` (MCP bridge) |
| `replay`, `dist`, `onboard` | replay recorded turns / workspace inventory / setup status |

Built-in channel types are `discord`, `slack`, and `telegram`; use `agentos
channels types` as the authoritative catalog. Config migration backs up the
file before removing entries for retired built-in channel types.

Telegram direct messages always require pairing. Use `agentos channels pairing
list <name>`, `approve <name> <code>`, `deny <name> <sender-id>`, or `revoke
<name> <sender-id>`. Pairing is binary and has no admin/owner tier. Telegram
groups are disabled by default; enable them only with explicit
`group_chat_ids`, paired senders, and the desired mention requirement.

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
(`AGENTOS_AGENT_PERMISSIONS`, `AGENTOS_GATEWAY_TOKEN`, …) cannot be written
through AgentOS; edit the file by hand if one is genuinely needed. When
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
| top-level | `workspace_dir`, `state_dir`, logging, `search_provider`/`search_api_key`, timeouts |
| `[x_search]` | xAI-backed X (Twitter) search: `enabled`, `model` (default `grok-4.5`), `api_key`/`api_key_env` (`XAI_API_KEY`), `reasoning_effort`, `timeout_seconds`, `total_timeout_seconds`, `retries`. The `x_search` tool is hidden until a credential resolves |
| `[llm]` | `provider`, `model`, `api_key`, `base_url`, `proxy`, `[llm.provider_routing]` |
| `[agentos_router]` | router on/off, `strategy` (`pilot-v1`), tier settings under `[agentos_router.tiers.c0..c3]` |
| `[skills]` | skill filtering/injection: `filter_strategy`, `filter_top_k`, `injection_mode`, `max_skills_prompt_chars` (default 24000), `max_skill_view_chars` (default 10000, 0 disables) |
| `[tools]` | model-visible tools and policy; `enabled = false` runs providers in plain-text mode; `profile` (`full` \| `coding` \| `messaging` \| `memory_only` \| `minimal`) sets the base allowlist — `agentos context` prices each one |
| `[memory]` | memory source and embedding model, `[memory.nudge]` (periodic memory review) |
| `[sandbox]` | `sandbox`, `default_level` (DISABLED/STANDARD/STRICT/LOCKED), `backend`, network/mounts |
| `[permissions]` | `default_mode` = `off` \| `on` \| `bypass` \| `full` (pair with `agentos sandbox …`) |
| `[auth]` | gateway admission: `mode` (`none` on loopback or `token`), `token` |
| `[control_ui]` | `allowed_origins` for reverse-proxy setups; `show_thinking` (default true) streams model reasoning to the WebUI as collapsible blocks — WebUI-only, channels never receive it |
| `[updates]` | `notify` (default true) — the once-per-24h "new release available" notice |
| `[channels]` | messaging channels (`[[channels.channels]]` entries) |
| `[auxiliary]` | model for work AgentOS runs itself, not the agent's turn (document analysis, image description): `provider`, `model`, `timeout_seconds`, `[auxiliary.tasks.<task>]`. Empty = reuse `[llm]` |
| `[prompt]` | prompt-layer flags: `platform_hint_enabled`, `env_probe_enabled` (local-toolchain block, names only) |
| `[compaction]`, `[agent_token_saving]`, `[task_runtime]` | context compaction, tool-result projection, concurrency |

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

Default port **18791**, loopback bind. `--listen HOST:PORT` overrides
`--bind`/`--port` together. `gateway status` (and `--json`) reports **both**
the installed CLI version (`cliVersion`) and the running gateway's version
(`gatewayVersion`); a `versionMismatch` diagnostic means the gateway is running
old code — restart it.

### Upgrading AgentOS

```sh
agentos upgrade                # upgrade, then restart + verify the gateway
agentos upgrade --check        # is a newer release available? changes nothing
agentos upgrade --dry-run      # print the command that would run; touch nothing
agentos upgrade --no-restart   # upgrade only; gateway keeps running OLD code
```

`agentos upgrade` is the primary path: it detects the install method and
installs the **published PyPI release** of `use-agent-os[recommended]` (`uv tool
install --force --python <running> …` / `pipx install --force …`), then by
default restarts the managed gateway and verifies it reports the new version
before declaring success. It never installs from a local checkout — even when
the current install came from one — because only `bash scripts/install_source.sh`
rebuilds the React control UI before installing, and a PyPI wheel already ships
a CI-built one. A checkout-backed install gets an informational note naming that
directory and the script; it never blocks. For pip / editable / unknown installs
it prints the exact manual command and exits non-zero (**exit 3**) rather than
faking it; a failed or unverifiable upgrade is **exit 1**. Flags: `--timeout`
(subprocess bound, default 600s; kills the process group on timeout),
`--config`, `--json` (adds `sourceDirectory`).

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

Unauthenticated non-loopback Control is unsupported. A reverse proxy, VPN, or
firewall does not replace the AgentOS Control token. Behind a reverse proxy on
another browser origin, also set `control_ui.allowed_origins`.

### Skills

```sh
agentos skills list                    # installed, per layer
agentos skills search <query>
agentos skills install <name>                    # from ClawHub (default source)
agentos skills install owner/repo:path -s github # from a GitHub repo/URL
agentos skills install <bankr-skill-url> -s bankr # from Bankr (repo or bankr.bot URL)
agentos skills tap add owner/repo      # register a GitHub repo as a skill source
agentos skills tap list
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
`--iteration-timeout-seconds`, `--tool-timeout-seconds`; containment:
`--workspace-strict` (reads), `--workspace-lockdown` (writes),
`--scratch-dir`.

### Day-two operations

```sh
agentos sessions list / show <id> / export <id> <out>
agentos cron list / add / run <id> / runs
# --job-kind decides what fires. Default 'auto' = reminder: --text is delivered
# verbatim and NO LLM runs, so a job that should think needs agent_turn.
agentos cron add --every 1h --job-kind agent_turn --text "Summarize updates"
# script jobs run a file in ~/.agentos/scripts/ and deliver its stdout — no
# model, no tokens. Empty stdout = silent; non-zero exit delivers the error and
# fails the job. Relative paths only; CLI/Web callers only (never a channel).
agentos cron add --every 5m --script watch-memory.sh --name memory-watchdog
# ...but a job added from the CLI has no chat to deliver into, so that stdout
# only reaches the run record. --session-key names the chat it reports into
# (the run stays isolated). Web UI / in-chat jobs already carry their session.
agentos cron add --every 5m --script watch-memory.sh --session-key "$KEY"
# 'runs' shows each run's Output + Delivery; Output is a 500-char preview.
# 'output' prints one run in full — latest by default, or --run <run-id>.
# Delivery 'fwd:no_session_target' == printed something, reached no chat.
# In chat, the cron tool reads the same history via action="runs" — use it to
# answer "what did that job do?" instead of guessing from the schedule.
agentos cron runs <id> [--json]
agentos cron output <id> [--run <run-id>] [--json]
# --script on an agent_turn is a pre-run collector instead: its stdout becomes
# the turn's context, and a tick that prints nothing skips the turn (no tokens).
agentos cron add --every 10m --job-kind agent_turn --script watch_rss.py \
  --script-arg --url --script-arg https://example.com/feed.xml \
  --text "Summarize anything urgent."
# Cron turns are read-only by default, so a job cannot run a shell-based skill.
# --elevated opts one agent-turn job out of that: no approval, no sandbox, host
# shell as the user. See docs/cli.md before suggesting it.
agentos cron add --every 6h --agent main --elevated --name "LP check" --text "..."
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
- **Skill missing from prompt** → do not guess from the layer. Ask the
  surface that knows: the `availability.reason` on a `skills.list` row, or the
  `[not offered] …` line the agent's own `skill_list` prints. Then act on the
  reason:
  - `ineligible` → the detail names the missing binary or variable;
    `agentos env set <NAME> --stdin` applies to the running gateway, no
    restart, and the skill becomes eligible on the next turn.
  - `prompt_budget` → the skills block is full. Raise
    `agentos config set skills.max_skills_prompt_chars <n>` (default 24000)
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
`features/skills`, `features/agentos-router`, `features/memory`.
