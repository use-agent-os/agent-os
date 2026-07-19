# Changelog

All notable changes to AgentOS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
