# Changelog

All notable changes to AgentOS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- A variable reported as missing is now checked against the places a
  credential may already live. If `gh auth login` has been run, `GITHUB_TOKEN`
  is reported as available from the GitHub CLI and can be imported with
  `agentos env import GITHUB_TOKEN` or a button on the Environment screen.
  Checking runs `gh auth status`, never `gh auth token`, so nothing reads a
  secret to decide whether one exists; importing only happens when asked for.
- When a skill's requirements are unmet, `skill_view` appends a setup note
  saying what is missing and what to do about it — and what to do depends on
  who is listening. A chat channel is told a secret must not be collected
  there because it would be stored in the conversation; an unattended run is
  told to continue and state what does not work; an interactive session gets
  the actual command. The skill still loads either way.
- Environment variables can be managed from AgentOS instead of by hand-editing
  `~/.agentos/.env` and restarting. Every surface that could already *detect* a
  missing variable can now *fix* it: a new **Environment** screen in the Web UI
  (`/env`), an `agentos env list|get|set|unset` command, `env.*` gateway RPC,
  and a **Set &lt;VAR&gt;** action in the Skills dialog next to the existing
  install action. Setting a variable applies it to the running gateway, so a
  skill that was ineligible for want of one becomes eligible without a restart.
- Skill manifests can describe the variables they need — a description, where
  to obtain the value, and whether it is a secret — instead of only naming
  them. Existing manifests using the plain `requires.env: [NAME]` list keep
  working unchanged.
- Skills can also declare non-secret settings under `metadata.agentos.config`,
  stored in the TOML config under `[skills.config]` rather than in `.env`.
  Their current values are appended to what `skill_view` returns, so the agent
  starts from what is configured instead of asking.
- The agent has `env_list` (names and set/unset state, never values) and, gated
  behind the approval queue and hidden by default, `env_set`. There is no
  reveal tool: a model that can read back stored credentials is one prompt
  injection away from exfiltrating them.

### Security

- Environment writes are refused for names that steer subprocess execution
  (`PATH`, `LD_PRELOAD`, `PYTHONPATH`, `EDITOR`, …) or AgentOS runtime posture
  (`AGENTOS_AGENT_PERMISSIONS`, `AGENTOS_GATEWAY_TOKEN`, `AGENTOS_STATE_DIR`,
  …). Every tool AgentOS spawns inherits `os.environ` and several guards are
  read from it, so a writable surface without this gate could widen what the
  agent is allowed to do. The gate applies on write only — values set in your
  shell or by editing the file directly keep working, and the `AGENTOS_` prefix
  is not blanket-blocked.
- Listings never carry a value. `env.reveal` is a separate method, rate limited
  to five per thirty seconds and written to the audit log.
- The Hermes migration wrote the migrated `.env` at the default umask, leaving
  imported credentials world-readable on a typical box. It now writes `0600`,
  like every other `.env` AgentOS creates.

### Upgrade notes

- Two `.env` lines that AgentOS previously ignored now take effect: a
  bash-style `export KEY=value`, and the first entry in a file saved with a
  byte-order mark. Both were parsed into unusable keys before (literally
  `export KEY`, and `\ufeffKEY`), so the variable was not set. If your `.env`
  has either, expect that variable to start being applied — which is what the
  line was written to do. Values exported in your shell still win over the
  file, so nothing that was already working changes.
- CLI logs now go to stderr instead of stdout. Anything capturing a command's
  stdout to collect log output needs `2>` instead; in exchange, `--json`
  output is parseable on an install that has a populated `.env`.
- The skill snapshot cache is invalidated once on first run, so the first
  command after upgrading rescans skills from disk.

### Removed

- The session-flush subsystem is gone. It wrote a "flush receipt" before
  destructive compaction and never earned its keep: roughly 8,000 lines for a
  memory path that underperformed. Compaction still records a durable
  checkpoint first, so the pre-image it recovers from is unchanged.
- The `memory.flush_*` and `memory.repair_*` configuration keys are no longer
  read. An existing `agentos.toml` keeps working — the keys are dropped on load
  with one warning naming them, and the file is rewritten on the next config
  save. They will be rejected outright in 0.2.0.

  Removing `memory.flush_enabled`, `memory.flush_compaction_safety_mode`, and
  `memory.flush_compaction_requires_safe_receipt` matters most. With no flush
  service left, no receipt can ever be written, so `flush_enabled = true`
  combined with `block` (or the legacy `requires_safe_receipt`) would have made
  compaction demand a receipt nothing could produce: refused on every turn,
  context window filling until the provider errors, with a single warning line
  as the only clue.
- `sessions.reset` and `sessions.contextCompact` no longer return a
  `flush_receipt` field, and `agentos reset` no longer prints a "Flush mode"
  line. Both described work that no longer happens.

### Fixed

- `env_key` is not always a variable name: providers that authenticate by
  OAuth carry the literal string `"OAuth"`, which put a variable called
  `OAuth` on the Environment screen that nobody could set.
- `skill_list` no longer tells the model to call `env_set`, which is hidden by
  default and so usually not callable — the same dead-end this feature exists
  to remove.
- A `.env` value with significant leading or trailing whitespace was written
  unquoted and then silently trimmed when read back. The OpenClaw migration
  carries a command allowlist across, and its entries are prefix patterns:
  `"^pytest "` with the trailing space matches that command, while `"^pytest"`
  without it matches anything starting with those six characters. Migrating an
  allowlist and quietly widening it is the wrong direction.
- `.env` parsing now recognises the bash-compatible `export KEY=value` form.
  A hand-written `export GITHUB_TOKEN=…` was previously invisible to AgentOS,
  and a save would have appended a second, competing definition.
- `/reset` in the standalone chat TUI works again on sessions with a non-empty
  transcript. It had been gated on a flush service that is never constructed,
  so it aborted every time; `/compact` printed a matching false warning.

## [2026.7.26] - 2026-07-26

### Added

- Curated memory now nudges itself. Every N user turns — default 10,
  configured at `[memory.nudge]`, `interval = 0` disables it — a short
  background review runs after the reply is already on the wire and saves
  anything durable it found in the conversation. Machine traffic (cron,
  heartbeat, subagent, recall), the review turn itself, and turns where the
  agent already wrote to memory are excluded and do not advance the counter.
- OpenCAP is supported as an LLM gateway provider.
- Telegram shows a typing indicator while a turn is running.
- `SECURITY.md` documents GitHub private vulnerability reporting as the
  intake path for suspected vulnerabilities.

### Changed

- **Breaking:** channel authorization is now two connection surfaces —
  Control and Channel — backed by explicit RPC audiences, replacing roles and
  scopes. Telegram pairing is durable, group admission is explicit, and
  grants are revalidated before turns and tools. Owner/admin elevation is
  gone from tools, cron, the CLI, and the Control UI; sandbox and approval
  policy are unchanged. Channel roles, scoped tokens, access modes, and
  unauthenticated public Control are removed — existing configs using them
  need to move to pairing surfaces.
- Cron job management is scoped to the active profile, so jobs from one
  profile are no longer listed or mutated from another.
- Daily notes that the injection budget would discard are no longer read at
  all, cutting per-turn memory I/O.

### Fixed

- `/new` and `/reset` are non-destructive when flush is unavailable: the
  session is no longer discarded on a path that cannot produce a receipt,
  and compaction only demands a flush receipt when flush can actually
  produce one.
- The `MEMORY.md` migration is non-destructive — an existing file is
  preserved rather than overwritten.
- Turn captures are written atomically, so an interrupted write can no
  longer leave a truncated capture behind.
- Curated memory writes are locked on Windows, the injection scan covers a
  wider set of paths, and the hermes durability guards in the curated store
  are restored.
- `USER.md` counts as a memory source for write notifications.
- Unreadable curated files are surfaced as errors instead of being silently
  skipped, which previously left the agent blind to memory it could not
  read.
- The degraded-source list no longer grows one entry per failed metric.
- Slack dispatches Socket Mode slash commands and classifies slash-command
  conversations correctly.
- Discord completes native interaction responses and tolerates command
  registration failures instead of failing adapter startup.
- Telegram handles native bot-command mentions, preserves forum command
  reply targets, renders markdown replies, allows admitted DM slash
  commands, and keeps pairing runtime state serializable.
- Admitted senders are granted read access across channels.

### Removed

- Dream consolidation, the orphaned `flush_status`, the memory repair
  service, and the `agentos memory flush-session` command are removed.

## [2026.7.25] - 2026-07-25

### Added

- Added one fail-closed Control UI build contract,
  `python scripts/build_control_ui.py build`, for local source installs, CI,
  Docker, wheel/sdist publication, and wheelhouse releases. It requires
  Node.js 22 or newer, performs a clean locked npm install, enforces bundle
  budgets, generates an exact third-party license ledger, and verifies the
   resulting React bundle before packaging.
- OpenRouter's `openai/gpt-5.6-luna` is now the default LLM model.

### Changed

- The production Control UI is now the React 19 + Vite application on every
  route. Release wheels, source distributions, Docker images, and wheelhouse
  archives carry the same prebuilt, verified bundle; a missing or invalid
  bundle returns an actionable `503` instead of silently serving a different
  interface.
- Repository source builds and the provided source-install scripts now require
  Node.js 22 or newer and npm so they can build the Control UI before
  installing the Python package. Published wheels remain ready to run without
  Node.js.
- The SPA shell and runtime bootstrap are uncached while fingerprinted Vite
  assets are served with immutable caching. A runtime-injected base element
  lets one artifact serve `/control` and safe non-root custom prefixes,
  including deep-link refreshes; root, `/api`, and `/ws` prefixes are rejected
  because they overlap gateway routes.
- Guided setup and advanced configuration now share one Agent Setup workspace
  at `/control/settings`; the existing `/control/setup` and `/control/config`
  URLs remain compatibility routes, while adapter onboarding and credential
  validation now live with channel status and access management.
- Configuration clients now read one redacted `config.snapshot` and submit
  optimistic `expectedRevision` writes through a shared persist-first
  transaction. The gateway reports cumulative restart reasons, preserves
  write-only secret semantics, and provides explicit recovery when runtime and
  on-disk state diverge.

### Security

- The packaged Control UI now uses a same-origin Content Security Policy
  without `unsafe-inline` scripts. Theme initialization runs from a packaged
  pre-paint script; HTTP requests stay same-origin while explicit `ws:` and
  `wss:` remote-gateway profiles remain supported.
- Configuration snapshots never return stored secret values, and stale Control
  UI drafts fail closed when the active configuration changes on disk instead
  of overwriting an operator's out-of-band edit.

### Removed

- Retired the DingTalk, Matrix, QQ Bot, and WeCom channel adapters across the
  runtime, CLI, Web UI, configuration schema, install metadata, and current
  documentation. Supported messaging adapters are now Slack, Telegram, and
  Discord.
- Removed the retired Jinja Control UI template and its hand-maintained
  JavaScript, CSS, fonts, images, and vendored browser libraries. There is no
  legacy frontend fallback at runtime or in release artifacts.

### Fixed

- Control UI settings preserve the active configuration state, Bankr icons
  render correctly, and resetting a session reliably clears its client state.
- The collapsed Control UI sidebar toggle has improved interaction and layout.
- CLI onboarding prompts wrap correctly instead of overflowing narrow terminals.

## [2026.7.23] - 2026-07-23

### Added

- Mouse drag selection and copy in the full-screen `agentos chat` transcript:
  left-drag highlights text in the transcript pane and mouse-up copies the
  plain text (ANSI stripped, CJK width-aware) to the system clipboard via a
  cross-platform dispatcher (pbcopy, wl-copy, xclip, xsel, clip, OSC 52
  fallback) (#76).
- Rendering for reasoning-model think blocks in the CLI, with hidden tags and
  boundary markers so partial think content streams cleanly.

### Changed

- The waiting indicator is now turn-lifetime: it persists across the pre-token,
  mid-stream, and tool-call phases to give a consistent "agent is working"
  signal. `StreamingRenderer` uses the waiting indicator instead of a Rich
  `Live` instance, which removes ghost panel artifacts in Windows PowerShell.
- Markdown streaming keeps block and inline styles intact while preserving the
  raw buffer for downstream consumers.

### Fixed

- Telegram no longer deletes its persistent native command menu on adapter
  shutdown. Bot command menus are server-side configuration and must survive
  gateway restarts and overlapping adapter lifecycles (#74, fixes #52).

## [2026.7.22.post1] - 2026-07-22

### Added

- Managed MCP server configuration in the Web UI, with stdio, SSE, and
  Streamable HTTP transports, OAuth authorization, dynamic tool discovery, a
  Robinhood Trading preset, and a bundled safety-focused Robinhood skill (#66).
- Mouse-wheel scrolling and Home/End and Ctrl+A/Ctrl+E line navigation in the
  full-screen `agentos chat` interface (#67).

### Changed

- Promoted the MCP SDK to a standard dependency so remote MCP integrations work
  without installing an optional extra (#71).
- Renamed the Web UI chat assistant label from `Cap` to `AGENTOS` (#73).

### Fixed

- `agentos chat` full-screen transcript now responds to the first mouse wheel
  tick instead of needing several scrolls before the pane moves: the wheel step
  is larger and the tick that releases follow mode is compensated so the
  wrapped-line cursor leaves the viewport immediately (#69).
- Unauthenticated OAuth MCP servers no longer connect during gateway startup;
  authenticated servers continue to reconnect automatically (#72).
- MCP cancellation cleanup now closes partial Streamable HTTP and discovery
  state so slow or unavailable remote servers cannot leave open AnyIO contexts
  or crash gateway startup (#71, #72).

## [2026.7.22] - 2026-07-22

### Added

- `tools.enabled = false` provides an explicit plain-text mode for Ollama and
  other models that do not reliably implement native tool calls.

### Fixed

- `agentos chat` input frame now supports multiline input instead of
  submitting on every `Enter` (#62).
- Ollama multi-turn tool conversations now preserve assistant tool calls,
  correlate tool results by name, normalize native arguments, and retain the
  provider's model and completion reason, preventing repeated searches caused
  by malformed replay history (#44).
- Channel slash commands now render their RPC results instead of returning a
  generic `/<command> completed` acknowledgement; `/help` and `/history` also
  request the correct catalog/history payloads.
- Telegram Bot API sends retry transient connection failures before reporting
  delivery failure.

## [2026.7.20] - 2026-07-20

### Added

- `agentos chat` UX pass (issue #46):
  - The assistant speaker label now defaults to `agentos` (was hard-coded
    `cap`); override with the `AGENTOS_ASSISTANT_LABEL` env var. The
    label is sourced from a single place and consumed by the streamed
    `◢` marker, the pre-token waiting row, and the queued-turn marker.
  - Session display name now surfaces in the bottom toolbar
    (`title · model · [tier:cN]`) and `/status`. `/new <title>` persists
    the title as `SessionNode.display_name` so it survives a later
    `/resume`. The standalone `/new` path no longer drops the title
    silently (pre-existing bug).
  - `/c0` … `/c3` and `/auto` are now registered on both CLI surfaces
    (`cli_gateway`, `cli_standalone`). Gateway mode reuses the existing
    `router.hold.set` / `router.hold.clear` RPCs; standalone mutates the
    in-process `RouterControlHoldStore` directly.
  - The active Pilot Router tier hold shows in the bottom toolbar and in
    `/status` (or `auto` when no hold is set).
  - `SessionNode.derived_title` property fills the pre-existing dead
    hook, falling back `display_name → label → short opaque session id`.
  - The startup panel now renders `Session: <title> (<key>)` when a
    friendly title is known (plumbed through `StartupData` and the
    gateway welcome notice).
  - The active input row is now framed by a top and bottom rule
    (Claude Code style) so the typing area reads as a distinct box
    between the transcript and the bottom toolbar. Consistent across the
    gateway and `--standalone` surfaces.
  - Full-screen chat surface is now the **default** for `agentos chat`: the
    conversation renders in a scrollable in-app pane above a permanently-pinned
    input frame, so the frame stays visible while the assistant streams (no
    flicker, no dropped partial lines). The branded welcome screen (connect
    line + banner + tool/skill panel) renders at the top of the pane on launch
    — previously it was wiped by the alternate screen buffer. `PgUp`/`PgDn`
    scroll history; new output re-pins to the tail. Non-TTY / piped
    invocations fall back to native scrollback; `AGENTOS_CHAT_FULLSCREEN=0`
    forces native scrollback and `=1` forces full-screen.

### Changed

- The bottom toolbar now leads with the session title (or short key
  fallback) instead of only the opaque key segment, and shows the model
  alias after it.

### Fixed

- The framed chat input no longer balloons to fill the screen on a fresh
  launch. The input buffer window is pinned to a single row
  (`Dimension.exact(1)`) and a greedy spacer heads the layout, so the
  compact frame + toolbar stay pinned to the bottom of the terminal
  instead of the bottom rule + toolbar being pushed far below the
  `◢ you` row.
- `test_assistant_label_env_override` no longer wipes the subprocess
  environment (`PATH=""`), which crashed Python startup on Windows CI
  (`import _overlapped` → `WinError 10106`); it now layers the override
  on a copy of `os.environ`.

## [2026.7.19.post1] - 2026-07-19

### Changed

- Renamed the router display name from "AgentOS Router" to "Pilot Router"
  across the CLI, gateway, and onboarding surfaces.
- Synced the router docs with the `pilot-v1` default and the 3-option
  strategy selector.

### Removed

- The legacy `v4_phase3` router engine and its ~52MB model bundle no longer
  ship (Phase C): the module, the bundled weights, and the `lightgbm` /
  `joblib` / `scikit-learn` dependencies are gone from the wheel and the
  `recommended` / `ml-router` extras. A config that still pins
  `strategy = "v4_phase3"` keeps migrating to `pilot-v1` on load, and the
  removed `v4_bundle_dir` / `v4_use_aux_head` keys are ignored.

## [2026.7.19] - 2026-07-19

### Added

- Bundled `agentos` self-operation skill so the agent can drive its own
  AgentOS CLI and gateway. (#37)

### Changed

- AgentOS Pilot (`pilot-v1`), the self-trained on-device English router, is now
  the default router strategy. (#26)
- Router strategy migration: persisted `v4_phase3` selections are force-migrated
  to `pilot-v1` at gateway boot, and `v4_phase3` is dropped from the
  human-facing onboarding and router selectors. (#36)
- Bankr skills browse source: limited to two curated skills to avoid GitHub rate
  limiting, filled the skill descriptions, and added a brand-glyph logo
  fallback, a 📺 emoji avatar fallback, and an "Update" button backed by the
  `skills.update` RPC. (#39, supersedes #35)

### Fixed

- Skills UI: the installed badge desynced between cards after an install and
  reverted to "not installed" after a page refresh — installed skills are now
  matched by both name and identifier. (#39)
- Skill browsing crashed on an explicit JSON `null` description returned from the
  GitHub/Clawhub search boundary; the description now defaults safely. (#39)
- Local single-provider setups keep self-consistent router tiers, and several
  local-provider degrade gaps were closed (vLLM handling, empty-model honesty,
  and log visibility). (#30)

## [2026.7.18.post1] - 2026-07-18

### Fixed

- Release-hygiene re-cut of 2026.7.18. The initial 2026.7.18 tag only bumped
  `pyproject.toml`, so the repo's release-consistency guards
  (`tests/test_release_consistency.py`, `tests/test_install_scripts.py`) failed
  and the install docs/scripts still pointed at the prior tag. This post-release
  propagates the version across `uv.lock`, both consistency tests, `RELEASES.md`,
  `CHANGELOG.md`, the README install examples, and `install.sh`/`install.ps1`.
  No runtime code changes — the distributed software is identical to 2026.7.18.

## [2026.7.18] - 2026-07-18

### Added

- Interactive authentication provisioning when the gateway binds to a public
  interface: instead of refusing to start, `gateway start` now provisions a
  token interactively so a public bind is authenticated by default. `host` and
  `port` are configurable only via CLI flags (not runtime RPC). (#25)
- Browser-threat hardening for the gateway (#24). A loopback bind is not a
  boundary against a page in the operator's browser, so four fail-closed
  guards were added: a startup guard that refuses `auth.mode="none"` on a
  non-loopback bind (opt out with `auth.allow_unauthenticated_public=true`),
  WebSocket-handshake Origin validation (CSWSH), a `Host`-header allowlist
  (DNS rebinding), and an HTTP cross-origin guard on `/api/*`. Runtime
  `config.apply`/`config.patch` of `host` or `auth.mode` now reports
  `restartRequired: true`, since a host change does not rebind the live
  socket.

### Changed

- **BREAKING (opt-in deployments only):** the gateway now refuses to start
  when `auth.mode="none"` is combined with a non-loopback bind
  (`0.0.0.0`, a LAN IP, ...). If you deliberately run an unauthenticated
  gateway behind a reverse proxy / VPN / firewall, set
  `auth.allow_unauthenticated_public = true` (or
  `AGENTOS_AUTH_ALLOW_UNAUTHENTICATED_PUBLIC=true`). Default loopback
  deployments are unaffected. (#24)
- **BREAKING (opt-in deployments only):** `auth.mode="trusted-proxy"` no
  longer satisfies the public-bind guard. It only string-matched the
  client-suppliable `X-Forwarded-For` header (spoofable) and has no
  end-to-end resolver, so it did not actually authenticate. Use
  `auth.mode="token"` on public binds until real peer-IP validation ships.
  (#24)
- Reaching a loopback gateway through a custom hostname (e.g. an
  `/etc/hosts` alias to `127.0.0.1`) or a reverse-proxied Control UI now
  requires adding that origin to `control_ui.allowed_origins`; otherwise the
  `Host`/Origin guards reject it. The rejection message names the config key.
  (#24)

## [2026.7.17.post1] - 2026-07-17

### Fixed

- The `session_status` tool no longer fails on every call in a running
  gateway. It called `SessionManager.get_current_session()`, a method that
  exists only on test fakes and never on the production `SessionManager`, so
  the attribute access raised `AttributeError` and surfaced as
  `ToolError: Session manager not available`. It now resolves the calling
  session from the tool context — the same source the surrounding session
  tools already prefer — and loads it via `SessionManager.get_session()`.

## [2026.7.17] - 2026-07-17

### Added

- Curated memory stores, embedding refresh, and a pluggable memory
  provider layer (mem0). (#17)
- Restored the missing v4_phase3 local ML router bundle so the default
  router runs on-device instead of pinning to a single class, and
  corrected its attribution to OpenSquilla upstream. (#19)

### Changed

- Redesigned the Web UI chat transcript. (#15)

### Fixed

- `agentos memory embedding-download` now follows Hugging Face's CDN
  redirects. Every `resolve/main/...` URL answers with a 302 to a signed
  Xet CDN URL, but `httpx` does not follow redirects by default, so the
  download aborted with an `HTTPStatusError` before writing any data and
  the command never worked against the live API. (#20)

## [2026.7.15.post1] - 2026-07-15

### Added

- Partner-catalog skills system with a Bankr skills hub, and a
  Robinhood RWA address lookup skill (`robinhood-rwa-addresses`).

## [2026.7.15] - 2026-07-15

### Changed

- Relicensed the repository from MIT to **Apache-2.0** and added a root
  `NOTICE` file. Core modules derived from
  [OpenSquilla](https://github.com/opensquilla/opensquilla) (Apache-2.0)
  are now credited in `THIRD_PARTY_NOTICES.md`; the README credits
  OpenSquilla (built on) plus OpenClaw and Hermes Agent (influences).
  Wheels now ship `LICENSE`, `NOTICE`, and `THIRD_PARTY_NOTICES.md` in
  their dist-info license files.

## [2026.7.14.post1] - 2026-07-14

### Changed

- The Python distribution is now published to PyPI as **`use-agent-os`**
  (`uv tool install "use-agent-os[recommended]"`). The import package
  (`import agentos`) and the `agentos` CLI are unchanged. PyPI's project-name
  similarity rules reject `agentos`/`agent-os` variants (the bare name is held
  by an unrelated, abandoned 2022 project), hence the org-matching name.
- Built wheels are named `use_agent_os-<version>-py3-none-any.whl` (PEP 427
  normalization). Install scripts, the wheelhouse builder, the release
  workflow, and the README now reference the new filename; the README's
  primary terminal install is the PyPI command instead of a pinned wheel URL.

## [2026.7.14] - 2026-07-14

### Changed

- Re-release aligning the current version tag to 2026.7.14.
- Adopted CalVer versioning (`YYYY.M.D`). Because PEP 440 normalizes the version
  segment in wheel filenames (leading zeros dropped), tags use the same
  non-padded form, e.g. `v2026.7.15`.
- Install docs outside the README (`README.product.md`, `docs/quickstart.md`,
  `docs/mcp-server.md`, `docs/operations.md`) now point to the canonical README
  Installation section instead of duplicating version-pinned wheel URLs.

## [0.0.1] - 2026-07-05

Initial release of AgentOS.

### Core

- `agentos` Python package with the `agentos` and `gateway` CLI entry points.
- Unified gateway: one local Starlette server (`127.0.0.1:18791`) drives a
  single `TurnRunner` engine shared by the Web UI, the CLI, and every chat
  channel (Slack, Telegram, Discord, DingTalk, WeCom, Matrix, QQ). Tool
  calls, retries, approvals, and logs behave the same on every surface.
- Durable sessions, chat history, and replay data persisted in SQLite, with a
  per-agent workspace folder and bounded-depth subagents.

### Pilot Router

- Pilot Router picks the cheapest capable model tier (c0–c3) for each turn.
  The default `recommended` install ships the router; `AGENTOS_INSTALL_PROFILE=core`
  or `--router disabled` turns it off and routes every turn to one model.
- Two selectable routing strategies. The default `v4_phase3` runs an on-device
  ML ensemble (BGE embeddings + LightGBM) that scores each turn locally with no
  LLM call; the `recommended` / `ml-router` extras install its runtime
  dependencies. Its ~75MB model bundle is kept out of git and is not
  distributed with the repo or the wheel in this release, so unless the bundle
  is restored locally the router degrades gracefully — it logs a warning at
  boot and pins every turn to the default tier. The alternative `llm_judge`
  strategy classifies each turn (R0–R3) via a small LLM call — a cloud model or
  a local OpenAI-compatible endpoint (Ollama / LM Studio / llama.cpp / vLLM)
  set with `judge_model` / `judge_base_url` — and needs no local model files.
- Onboarding (Web UI wizard and CLI) offers the strategy via the Mode dropdown —
  "Pilot Router (Local ML)", "Pilot Router (LLM Judge)", or "Disabled". The
  "Judge model" field applies to, and appears only for, the LLM Judge strategy.
- `/c0`–`/c3` slash commands (web chat and messaging channels) pin the router
  to a tier for the current session; `/auto` restores automatic routing. These
  share the same short-lived hold store as the LLM-facing `router_control`
  tool via the `router.hold.set` / `router.hold.clear` gateway RPCs.
- The router auto-select visualisation mounts in a dock directly below the
  chat input bar and shows the latest turn's routing state.

### Providers

- Talks to 20+ LLM providers behind one config. **OpenRouter** is the default
  (`llm.provider = "openrouter"`, base URL `https://openrouter.ai/api/v1`,
  env `OPENROUTER_API_KEY`). The **Bankr LLM Gateway**
  (`https://llm.bankr.bot/v1`, env `BANKR_API_KEY`) is a selectable
  OpenAI-compatible gateway with its own tier profile. OpenAI, Anthropic,
  Ollama, DeepSeek, Gemini, DashScope/Qwen, Moonshot AI, Zhipu, Baidu Qianfan,
  and Volcengine Ark are also onboarding-verified.
- Model catalogs are fetched live from the provider's public endpoint at boot
  (context window, max output, vision support), with a hardcoded static
  fallback retained for offline boots.
- The `/model` slash command lists available models (name, id, provider,
  context window) across the TUI, web chat, and channel surfaces, with an
  optional `/model <filter>` substring filter.

### Tools, skills, and memory

- MCP-native tools and 37 bundled skills (coding, GitHub, cron, pptx/docx/xlsx/pdf,
  summaries, tmux, weather, and more) that load only when a task needs them.
  AgentOS can consume other MCP servers and expose itself as one
  (`agentos mcp-server run`, `mcp` extra).
- Persistent local memory: a `MEMORY.md` file plus dated Markdown notes,
  searchable by keyword (SQLite FTS) or meaning (`sqlite-vec`). Semantic recall
  runs on-device via a bundled BGE ONNX embedding model
  (`src/agentos/memory/models/bge_onnx/`), or can defer to OpenAI / Ollama.
- Built-in web search (Brave or DuckDuckGo) with SSRF-safe page fetching,
  document generation (PPTX/DOCX/PDF), image generation, and text-to-speech.

### Security and operations

- Layered security sandbox with three levels (Standard, Strict, Locked):
  Bubblewrap on Linux, `sandbox-exec` (Seatbelt) on macOS. Repeated denials
  auto-pause the agent; blocked output and tool results are sanitized so they
  cannot steer the model.
- Operator controls: human approval for risky tool calls, per-turn and
  per-session token/cost accounting (`agentos cost`), and diagnostics from both
  the CLI and Web UI (`agentos doctor`, the Web UI Health page).
- A `SchedulerEngine` with a built-in cron reader runs jobs via `agentos cron`.
- Config is auto-discovered (`AGENTOS_GATEWAY_CONFIG_PATH` → `./agentos.toml`
  → `~/.agentos/config.toml` → built-in defaults); environment-variable secrets
  always win over file values.
- One-way import from OpenClaw (`~/.openclaw`) and Hermes Agent (`~/.hermes`)
  via `agentos migrate`, with dry-run reports before applying.

### Brand and contribution

- Brand identity: the AgentOS wordmark and molecule mark.
- Plain pull-request contribution flow targeting `main`; relicensed to MIT.
