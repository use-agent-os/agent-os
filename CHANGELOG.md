# Changelog

All notable changes to AgentOS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Fixed

- Every session removal path drops the in-memory runtime state keyed by that
  session, not just `SessionManager.finish()`. `sessions.delete` (the Web UI
  "Delete Chat"), `SessionManager.cap_entries()`, `prune_stale()` and the cron
  `SessionReaper` all went straight to `storage.delete_session()`, leaving
  orphaned entries in three process-global stores — `SpawnGroupTracker`'s
  closed/woken sets, the Pilot router's per-session routing history, and the
  per-parent spawn locks. On a gateway that stays up for weeks, every deleted
  or pruned session leaked another entry with nothing to bound the growth.
  Eviction now lives in `agentos.session.runtime_state`, is idempotent, and
  runs on all of them. `sessions.delete` also cancels and drains the session's
  active and queued tasks first, so an in-flight turn handler can no longer
  write to a session whose rows are about to disappear or repopulate the state
  just evicted; `prune_stale` collects the stale keys via the new
  `SessionStorage.list_stale_session_keys()` before deleting, since the
  storage-level prune returned only a count
  ([#750](https://github.com/use-agent-os/agent-os/issues/750)).
- `DiscordChannel` reconnect is now bounded and cannot silently kill the gateway
  pump. All three `_reconnect()` call sites in `_dispatch_loop`
  (`channels/discord.py`) ran unguarded inside a bare task, so a raising
  `_reconnect()` killed the pump while `_connected` stayed `True` — the channel
  looked healthy while no events were dispatched. `_reconnect()` now contains
  `_do_reconnect()` failures, retries with exponential backoff bounded by
  `reconnect_max_retries` / `reconnect_base_delay_s`, and marks the channel dead
  (`_connected = False`) once the budget is exhausted so the `ChannelManager`
  retry path takes over
  ([#1133](https://github.com/use-agent-os/agent-os/issues/1133)).

## [2026.9.5] - 2026-09-05

### Added

- Direct provider endpoints price from native vendor rates instead of falling
  through to the `_DEFAULT_PRICING` placeholder. Bare model ids without a vendor
  prefix (`deepseek-chat`, `deepseek-reasoner`, `gemini-2.0-flash`) and
  date-stamped snapshots (`claude-3-7-sonnet-20250219`, `gpt-4o-2024-08-06`)
  failed the `startswith` prefix scan and were billed at $3.00/$15.00 per 1M
  tokens — up to a 21x error in usage tracking and spend rollup for a cheap
  model. Direct rates for DeepSeek, Anthropic, Google Gemini and OpenAI are now
  registered alongside their prompt-cache read discounts
  (`cached_input_per_m`), snapshot suffixes are stripped before lookup, and
  candidate normalisation is provider-aware. Resolution is scoped to direct
  endpoints, so aggregator routing and the existing static table baseline are
  unchanged, and Opus keeps its tiered rates
  ([#842](https://github.com/use-agent-os/agent-os/issues/842)).

### Fixed

- `TaskRuntime` retains the cached routing envelope while a session has queued
  or running tasks, evicting it only after the final task for the session reaches
  a terminal state. This prevents proactive or follow-up sends from losing
  channel, account, recipient, and thread routing during multi-turn workflows
  ([#930](https://github.com/use-agent-os/agent-os/issues/930)).
- `SessionStorage` serializes every runtime method that can commit on the shared
  SQLite connection. Two coroutines writing concurrently could interleave inside
  a multi-statement transaction — one committing another's half-written state —
  and a mutating method that raised or was cancelled left the transaction open
  for whoever committed next. Write ownership is now held across a complete
  transaction and an open transaction is rolled back when the method exits with
  an exception, cancellation included. Migration writes stay outside the lock:
  they run sequentially during connect, before the storage instance is exposed
  ([#891](https://github.com/use-agent-os/agent-os/issues/891)).
- `JobStore.transaction()` rolls back on any `BaseException` instead of leaving
  uncommitted writes in the shared connection's buffer for a later caller to
  commit, and a task-bound reentrant write lock serializes `save`, `delete`,
  `save_execution`, `prune_runs` and `_reserve_job_for_run` so a concurrent
  writer can no longer commit incomplete batch state out from under an open
  transaction. Intermediate commits are deferred while a transaction is active
  ([#964](https://github.com/use-agent-os/agent-os/issues/964)).
- `SessionWriteLock` evicts a session's entry on `release()` when no acquirer is
  queued behind it. `_locks` never removed anything, so a long-running gateway
  retained one `asyncio.Lock` per unique session key for the life of the
  process; the dict is now bounded by currently active keys rather than every
  key ever seen. Entries with queued waiters are kept so lock handoff is
  unaffected ([#966](https://github.com/use-agent-os/agent-os/issues/966)).
- Auto Pilot falls back across the configured router tiers when a model times
  out or returns a pre-content error, instead of retrying the same dead endpoint
  three times and freezing the turn for roughly six minutes. Transport timeouts
  are classified apart from transient blips, the timeout retry is capped at one,
  a `provider_timeout_retry` warning is emitted, the fallback chain is derived
  from the active router tiers, and the terminal error now names the `/c0`,
  `/c2` and `/auto` escapes
  ([#860](https://github.com/use-agent-os/agent-os/issues/860)).
- The session FTS query sanitizer keeps non-ASCII letters. `[^a-zA-Z0-9\s]`
  stripped every accented Latin, CJK, Cyrillic, Vietnamese and Arabic character
  before the query reached FTS5, so `café déploiement` searched for
  `"caf" "d" "ploiement"` and `中文 报告` searched for nothing at all — while the
  transcripts themselves were indexed correctly. The pattern is now the
  Unicode-aware `[^\w\s]`, which still strips FTS5 operators
  ([#903](https://github.com/use-agent-os/agent-os/issues/903)).
- The Discord adapter no longer cancels itself while reconnecting. When
  `_heartbeat_loop()` detected a missed ACK and drove a reconnect,
  `_do_reconnect()` unconditionally cancelled `self._heartbeat_task` — the very
  task it was running inside — so the coroutine died at the next `await` during
  socket cleanup, before a new WebSocket or a replacement heartbeat task
  existed. The cancel is now skipped when the heartbeat task is
  `asyncio.current_task()`; externally initiated reconnects and adapter
  shutdown still cancel it
  ([#882](https://github.com/use-agent-os/agent-os/issues/882)).
- `list_dir` survives a broken symlink. A dangling link is not a directory, so
  the size lookup fell through to `entry.stat()`, which follows the link and
  raised an unhandled `FileNotFoundError` that took down the whole listing. The
  size query now falls back to `entry.lstat().st_size`, or `0`, on `OSError`
  ([#844](https://github.com/use-agent-os/agent-os/issues/844)).
- `agentos cost --export <path>` creates missing parent directories instead of
  raising `FileNotFoundError`, matching what `render_savings_pdf` already did.
  Both the JSON and CSV branches are covered
  ([#846](https://github.com/use-agent-os/agent-os/issues/846)).
- The gateway debounce buffer is capped at 50 coalesced messages per
  `session_key` and flushes immediately on reaching the cap, rather than
  accumulating without bound. The cap-triggered flush retains its delivery task
  and drains it on shutdown
  ([#796](https://github.com/use-agent-os/agent-os/issues/796)).
- Named artifact delivery falls back to a valid filename leaf. When an
  artifact's metadata carried an empty, whitespace, root or dot-relative target
  (`""`, `"   "`, `"/"`, `"."`, `".."`), `Path(filename).name` resolved to `""`
  and the delivery target became the temporary directory itself — the hardlink
  failed with `FileExistsError` and the `shutil.copy2` fallback handed
  `send_file` a directory path. The leaf is now sanitized, falling back to the
  source name or `artifact`
  ([#742](https://github.com/use-agent-os/agent-os/issues/742)).
- `robinhood-chain-stocks` withholds price and holding value for a contract it
  has already disproven. When `uiMultiplier()` reverted — `isStockToken: false`,
  which `SKILL.md` documents as "not a Stock Token, do not hand over the
  address" — an impersonator reusing a listed ticker still had the real
  company's live Chainlink feed attached to it, and `holding.valueUsd`
  calculated from it, lending a proven fake the credibility of a real price.
  Price and USD holding value are now withheld with a `readErrors` explanation;
  `isStockToken: null` still resolves a price, because an unreachable RPC node
  is not proof of fakery
  ([#866](https://github.com/use-agent-os/agent-os/issues/866)).

### Security

- `code_exec` detects destructive calls through the AST, not just the regex
  fast path. `_check_code_destructive` matched shallow patterns only, so
  reflection and dynamic-import constructs reached the host filesystem without
  passing the approval gate: `getattr(os, "rem" + "ove")(path)`,
  `__import__("os").remove(path)`, `importlib.import_module("os").remove(path)`,
  `exec`/`eval` of a destructive string, `from os import *`, aliased imports and
  aliased functions. A visitor now runs whenever the regex does not match,
  resolving statically computable strings (constants, concatenations, f-string
  values), tracking imports and aliases for `os`, `shutil`, `pathlib`,
  `subprocess` and `importlib`, and flagging dynamic `getattr` and `__import__`
  targets. The layer is additive — existing pattern coverage is unchanged
  ([#848](https://github.com/use-agent-os/agent-os/issues/848)).

## [2026.9.4] - 2026-09-04

### Fixed

- `agentos config set skills.config.<skill>.<key>` persists again. `_set_key`
  only overwrote keys already present in `to_toml_dict()`, and an empty
  `skills.config` is omitted there for rollback compatibility, so the documented
  command could never create the map. Missing intermediate dicts are now created
  under `skills.config` only, and unknown keys outside that map stay rejected.
  The no-`--config` path stopped lying too: it printed a fabricated
  `AGENTOS_GATEWAY_` export and exited 0 for keys that do not bind — including
  `gateway.port` and every `skills.config.*` key — so a user followed the hint
  and set an environment variable that nothing reads. Keys are validated against
  the model first, and the free-form `skills.config` map, which has no env
  binding, is refused outright
  ([#834](https://github.com/use-agent-os/agent-os/issues/834)).
- `load_entries` skips malformed lines in the decisions JSONL instead of raising
  on the first one. The file is append-only and written once per turn, so a
  SIGKILL mid-turn, an OOM or a disk-full error can leave a truncated line
  behind — and `load_entries` is the shared reader for cost-savings reports,
  session export and pipeline replay, all of which died together. The realistic
  corruption is not a bad string but a wrong-shape payload, which surfaces as
  `ValueError`/`TypeError` out of `_filter_payload` rather than
  `JSONDecodeError`, so all of them are caught. Skips are accounted for: one
  debug event per line with path, line number and error class, and one warning
  with the totals at the end, so a partial report announces itself instead of
  quietly under-reporting. This matches the tolerance
  `decision_log_aggregate.parse_log_line` already had, so the two readers of the
  same file now agree on what is fatal
  ([#812](https://github.com/use-agent-os/agent-os/issues/812)).
- An MCP client disconnecting no longer takes another client's tool with it.
  When two servers registered the same tool name, disconnect unregistered the
  name unconditionally, so the surviving client's tool vanished from the
  registry. Each active client's exact registry spec and handler are tracked; a
  colliding tool is unregistered only when the disconnecting client owns the
  active handler, and otherwise the most recently registered handler from a
  still-active client is restored
  ([#801](https://github.com/use-agent-os/agent-os/issues/801)).
- `background_process` output is capped at 1,000,000 retained characters per
  session, evicting older chunks so the most recent tail survives. Draining
  continues past the cap, so a noisy subprocess cannot block on a full pipe, and
  the retained character count and truncation state are exposed in the process
  session and log payloads
  ([#803](https://github.com/use-agent-os/agent-os/issues/803)).
- Provider credit exhaustion is classified as `INSUFFICIENT_CREDITS` rather than
  a transient fault. OpenAI returns `insufficient_quota` with HTTP 429, which
  read as `RATE_LIMITED` and tripped the circuit breaker for a billing fault no
  cooldown can heal; Anthropic returns `billing_error` with HTTP 402, which read
  as `UNKNOWN` and carried no recovery hint. A cross-provider
  `_is_insufficient_credits()` check runs before the status-code branch, so the
  raw code and message win over the ambiguous 429
  ([#777](https://github.com/use-agent-os/agent-os/issues/777)).
- CLI JSON output survives a non-UTF-8 terminal encoding.
  `json.dumps(..., ensure_ascii=False)` emits raw non-ASCII, and on a Windows
  code page (cp1252, cp437) `sys.stdout.write` raised `UnicodeEncodeError`.
  `_write_json_text` writes UTF-8 bytes to the underlying binary buffer when one
  exists — lossless, so the JSON contract holds for pipes and files — and falls
  back to the text layer with `errors="backslashreplace"`, which keeps the data
  as round-trippable `\uXXXX` escapes instead of destroying an em dash into `?`
  ([#764](https://github.com/use-agent-os/agent-os/issues/764)).
- Memory-write refresh callbacks reach the running turn.
  `build_turn_runner_from_services` never populated `svc._turn_runner_ref`, so
  `_on_memory_write` had nothing to call and `refresh_memory_snapshot(agent_id)`
  never ran on the active `TurnRunner`
  ([#761](https://github.com/use-agent-os/agent-os/issues/761)).
- `apply_patch` records `UpdateFile` in `ctx.workspace_file_writes`. Only
  `AddFile` was recorded, so a patch that edited an existing file left the
  engine's auto-publish path with nothing to publish, even though `UpdateFile`
  is a peer of `AddFile` everywhere else in the module. The parser also accepts
  the optional line counts in a standard `@@ -a,b +c,d @@` hunk header
  ([#753](https://github.com/use-agent-os/agent-os/issues/753)).
- `parse_version()` understands a bare `.dev` suffix and sorts dev
  pre-releases per PEP 440. There was a fallback defaulting a bare `.post` to
  `0` but none for `.dev`, so `2026.7.18.dev` parsed with `dev = None`, fell
  through to the final-release phase and compared equal to `2026.7.18` —
  suppressing the `is_newer()` update notice for every development install
  ([#740](https://github.com/use-agent-os/agent-os/issues/740)).
- Email is marked seen after the message is converted, not before, so a failure
  mid-conversion leaves the message unread and eligible for the next poll
  ([#719](https://github.com/use-agent-os/agent-os/issues/719)).
- `robinhood-chain-stocks` handles a non-dict RPC error payload. `_eth_call`
  assumed `error` was a mapping and crashed when a node returned a plain string
  ([#815](https://github.com/use-agent-os/agent-os/issues/815)).
- `gmgn-wallet-score` prints usage instead of crashing. `score.py` indexed
  `sys.argv[1]` and `sys.argv[2]` unguarded, so running it with too few
  arguments raised an unhandled `IndexError`; it now validates argument count,
  exits 2 with usage on stderr, and answers `-h`/`--help` with exit 0
  ([#819](https://github.com/use-agent-os/agent-os/issues/819)).
- Frontend line endings are normalised so Prettier stops failing on Windows
  checkouts. `.gitattributes` marks frontend sources `text=auto` — not a blanket
  `eol=lf`, which would have flagged PNG, webp and woff2 assets as text and
  corrupted them — and Prettier is configured with `endOfLine: "auto"`
  ([#825](https://github.com/use-agent-os/agent-os/issues/825)).

### Security

- Invisible Unicode characters are normalised before intent-phrase matching. A
  soft hyphen, word joiner, zero-width space or bidi isolator placed between two
  words split the intent-phrase regexes, so a prompt-injection payload evaded
  the guard entirely in both report and enforce mode. `classify_injection` now
  normalises invisible codepoints to a space before matching the non-invisible
  patterns, while `invisible_char` is still matched against the original text so
  the smuggling technique itself is reported rather than erased
  ([#690](https://github.com/use-agent-os/agent-os/issues/690)).
- Search results carry their provider origin, so text returned by a search
  backend is attributable when the injection guard inspects it
  ([#688](https://github.com/use-agent-os/agent-os/issues/688)).
- Per-IP rate limiting covers the Control UI API subtree.
  `RateLimitMiddleware._is_ui_path()` exempted the entire Control UI prefix,
  including everything mounted under `{base_path}/api/*`, so
  `/control/api/sessions`, `/control/api/chat` and `/control/api/config` took
  unlimited unauthenticated requests. It now mirrors the check
  `AuthMiddleware._is_ui_path()` already had
  ([#748](https://github.com/use-agent-os/agent-os/issues/748)).
- `send_file` checks file size before reading. Every channel adapter opened or
  read the file first, so a large attachment meant memory exhaustion — the
  email adapter base64-expands the whole payload in memory — or a long upload
  that ended in an API rejection. `check_channel_file_size` stats the file up
  front against each service's real ceiling (Discord 10 MB, Telegram 50 MB,
  email 25 MB) and raises with the limit named
  ([#683](https://github.com/use-agent-os/agent-os/issues/683)).
- `robinhood-chain-stocks` rejects a non-`http(s)` `--rpc-url`. The URL reached
  the HTTP layer unvalidated, so a `file://` URL turned an RPC call into a local
  file read; empty URLs and a bare `http://` are refused as well
  ([#816](https://github.com/use-agent-os/agent-os/issues/816)).

## [2026.9.3] - 2026-09-03

### Added

- **Inline card grids in Web chat** — a second AgentOS-native artifact mime,
  `application/vnd.agentos.cards+json`, alongside the existing chart one. A
  skill publishes a JSON payload and the transcript renders a responsive grid of
  record cards, each with an optional logo, a colour-coded status badge, and
  per-field copy buttons — instead of a download chip.

  This is the shape a markdown table handles badly: a 42-character contract
  address forces the table into a horizontal scroll, while a card gives the
  address its own line next to a copy button. `badgeTone` accepts
  `positive`/`warning`/`danger`/`neutral` and falls back to `neutral` for
  anything else, so a skill can introduce a new status without waiting on a
  frontend release. At most 24 cards render and the remainder are counted and
  reported under the grid rather than dropped silently.

  Every payload string reaches the DOM through `textContent`, never
  `innerHTML`, and `logo` is restricted to `http(s)` URLs — card fields carry
  on-chain metadata, which is attacker-controlled on a permissionless chain.

  `robinhood-rwa-addresses` is the first consumer: `scripts/rwa_cards.py` reads
  the lookup's JSON on stdin and emits the artifact, so an address answer in the
  Web UI arrives as a grid with the verification badge attached to each result.
  `docs/artifacts-and-media.md` documents the payload.

- **Skills publish their own artifacts.** `exec_command` now honours a
  `publish_artifact path=<file> mime=application/vnd.agentos.<x>+json` marker on
  a command's own output, so a skill that writes a chart or card payload gets it
  rendered without the model deciding to publish it. Live-testing the card
  renderer produced the same outcome seven times across two models: the script
  ran, the payload was written, and the answer came back as a hand-written
  markdown table with the artifact stranded in the workspace — a render that
  only happens when the model feels like it is not a contract.

  Only the `application/vnd.agentos.` family auto-publishes, so ordinary command
  output cannot push a workspace file at the user; a plain file still needs a
  deliberate `publish_artifact` call. The marker must own its line, so prose
  mentioning it is inert; at most four publish per command, with the overflow
  reported rather than dropped; and `publish_artifact`'s workspace containment
  is unchanged. The whole path is best-effort — a shell command never fails, and
  never loses its output, because a publish did not work out. This also fixes
  the existing `gmgn-token` and `gmgn-market` chart artifacts, which had the
  same failure mode. Both Robinhood skills now write their card payload on every
  run (`<SYMBOL>.cards.json`, marker on stderr so stdout stays pure JSON,
  `--no-cards` to opt out), and `robinhood-chain-stocks` gains
  `scripts/chain_cards.py`.

- Cards identify their subject with a locally drawn ticker monogram. The card
  grid has a logo slot, but the console's CSP is
  `img-src 'self' data: https://raw.githubusercontent.com`, so a token-list CDN
  image is blocked outright and the card was quietly dropping the broken `img`
  and showing nothing. Widening the CSP would also tell that CDN which tickers a
  user is researching, from their IP — a real leak on a finance surface, for
  decoration. The monogram needs no request and no trademarked artwork; the
  `logo` img is still attached and still takes over, but only on a real `load`,
  so an `error` now leaves the monogram standing instead of an empty slot.

### Fixed

- Telegram Bot API calls now retry `ConnectTimeout` and `PoolTimeout` alongside
  `ConnectError`. All three happen before any request bytes reach Telegram — a
  DNS/TLS handshake that never completed, or a wait for a pooled connection —
  but the two timeouts are `TimeoutException` siblings of `ConnectError` rather
  than subclasses, so `TelegramChannel._api()` dropped them into its generic
  `RequestError` branch and raised on the very first attempt with zero retries.
  `ReadTimeout` stays out of the retry path on purpose: by then the request is
  in flight, and re-sending a `getUpdates` long poll would double-poll it.
  ([#651](https://github.com/use-agent-os/agent-os/issues/651))
- **`robinhood-rwa-addresses` now verifies every address against Robinhood
  Chain instead of trusting the token index.** The skill decided what counted
  as a genuine Stock Token from a name suffix in CoinGecko's list, which was
  wrong in both directions. CoinGecko caps `name` at 60 characters, so long
  listings lost the "• Robinhood Token" marker mid-word and were dropped
  entirely — `--query IBM` returned no matches at all, as did VTI, XLK, CTSH
  and CRDO. In the other direction, 47 of the 238 entries the skill reported as
  verified Stock Tokens (JPM, MCD, DIS, UBER, ABNB, PYPL and others) have **no
  contract deployed at the advertised address**; the skill handed them out as
  usable addresses, and funds sent to one would be unrecoverable.

  Discovery still ranks candidates from the token list, but the answer is now
  settled on chain: every genuine Stock Token is a proxy pointing at Robinhood's
  shared EIP-1967 beacon `0xe10b6f6b275de231345c20d14ab812db62151b00`, which a
  permissionless impersonator cannot forge. One batched JSON-RPC round-trip
  (`https://rpc.mainnet.chain.robinhood.com`, no key, ~0.5s) classifies each
  match as `verified`, `not-deployed`, `not-a-stock-token`, or `unverified`,
  and a top-level `warning` carries the caveat. Undeployed listings are still
  returned — silently dropping them reads as "the skill is broken" — but are
  flagged and never presented as usable addresses.

  Following `robinhood-chain-stocks`, an unreachable node yields `unverified`
  rather than a negative verdict: a network fault is never reported as evidence
  that a token is fake. `--no-verify` skips the check explicitly and says so in
  its own wording, and `--rpc-url` points at an alternate node. The name-suffix
  match is retained only as the offline fallback, now tolerant of truncation.

- TaskRuntime queue depth gauge (`agentos_queue_depth`) now decrements when
  tasks leave the pending queue, instead of staying stuck at the peak enqueue
  value (#668).
- The sensitive-path hard block now refuses destructive intents that target
  the filesystem root. `rm -rf /` carries no sensitive *prefix*, so the
  denylist never matched it and a whole-host wipe fell through to the ordinary
  approval flow — which `/elevated bypass` skips outright. Every spelling that
  resolves to or sweeps the top level is covered: `/`, `//`, `/.`, `/..`,
  `/*`, `/*/*`, `/**`, `/?*`, `/.*` and `/[a-z]*`. Globs that name a subset
  (`/tmp*`) are untouched, and root counts as sensitive only in the
  delete-intent scan — reading or listing `/` stays ordinary work (#563).
- The image tool now reports a redirect that carries no `Location` header
  instead of the confusing failure it caused downstream. `_fetch_image_url`
  follows redirects itself so every hop is re-validated against the SSRF guard;
  a 3xx with no `Location` closed the response and fell out of the loop, so the
  failure surfaced as httpx's generic `Failed to fetch image from URL: Redirect
  response '302 Found' for url ...` (or a `StreamClosed` from reading the body
  that had just been closed, depending on the httpx version) rather than the
  dead-end hop that actually broke. It now raises `Redirect response from <url>
  missing Location header`, naming the URL that returned it.
- Channel HTTP retries now cover every transient timeout, survive an
  HTTP-date `Retry-After`, and hand back an exhausted rate limit.
  `retry_request` caught `(ConnectError, ReadTimeout)`, but `ConnectTimeout`,
  `WriteTimeout` and `PoolTimeout` descend from `TimeoutException` — a sibling
  of `ConnectError` under `TransportError` — so a DNS, TLS-handshake, upload or
  connection-pool timeout on any Slack/Discord/Telegram/webhook call escaped
  the backoff and crashed the caller on the first stall; the clause is now
  `(ConnectError, TimeoutException)`. `Retry-After` was parsed with a bare
  `float()`, so the HTTP-date form RFC 7231 §7.1.3 permits turned a rate limit
  into a `ValueError` inside the retry loop: the header is now resolved as
  delay-seconds or HTTP-date, falls back to the computed backoff when it is
  unparseable, non-finite, negative or already past, and is clamped to 300s so
  a provider cannot park a send for hours. The 429 branch also gained the
  `attempt < max_retries` guard the 5xx branch already had, so an exhausted
  rate limit returns the response — status, headers and provider error body
  intact — instead of sleeping once more and raising a bare
  `RuntimeError("retry_request exhausted")` (#642, #599).
- The email channel can poll an IMAP folder whose name contains spaces.
  `imap_folder` was handed to `imaplib` verbatim, and `imaplib` does not quote
  mailbox arguments, so a folder such as `Sent Items` — ordinary on
  Exchange/Outlook — went on the wire as two tokens and every poll failed with
  an opaque `BAD [CLIENTBUG] Invalid syntax`. The name is now emitted as an
  RFC 3501 quoted-string, escaping `\` and `"`, and a name carrying a control
  character (a CR or LF would have ended the command line and run its tail as a
  second IMAP command) is refused at channel start instead of at poll time.
- `SubscriptionManager._message_subs` now removes empty sets on
  unsubscription and connection teardown, preventing a slow memory leak
  on long-running gateways (#609).
- An email reply no longer drops the thread root when the inbound message
  carries no `References` header. `_merge_references` read only `References`,
  so for the second message of a thread — where most mail clients send
  `In-Reply-To` alone — the parent id was discarded and the outgoing reply
  referenced only itself, breaking the conversation apart in Gmail, Outlook and
  Thunderbird. The chain now falls back to `In-Reply-To` when `References` is
  absent, per RFC 5322 3.6.4. Both threading headers are now read by one
  parser that drops comments and accepts ids with or without angle brackets,
  and `thread_key_for` shares it, so the thread cache key and the reference
  chain can no longer disagree about which message is the root (#620).
- The Environment view's path strip shortens Windows paths again. `shortPath`
  split on `/` only, so a gateway-reported `C:\Users\<name>\.agentos\.env` counted
  as a single segment and was rendered untrimmed, overflowing the header strip
  it was written to keep short. Backslashes are normalised before splitting, so
  Windows and mixed-separator paths trim to their last two segments like POSIX
  ones do.
- Provider content-moderation blocks are classified as `POLICY_REFUSAL` again
  instead of falling through to `BAD_REQUEST`/`UNKNOWN`. `_is_policy_refusal()`
  held only generic phrasing, so the wording providers actually emit went
  unmatched: Azure OpenAI's canonical *"triggering Azure OpenAI's content
  management policy"* does not contain the adjacent words "content policy", the
  OpenAI/Azure `content_filter` code and `finish_reason` matched nothing, and
  Gemini's "blocked by safety" is not "safety policy". Since a refusal and a
  malformed request map to different recovery actions, the misclassification
  sent real policy blocks down the wrong path. Added `content_filter`,
  `content filter`, `responsible_ai_policy`, `content management policy` and
  `blocked by safety` (#629).

- Cron schedules that restrict both day-of-month and day-of-week now follow the
  POSIX OR rule instead of ANDing the two fields. `0 0 1,15 * 5` means "the 1st,
  the 15th, or any Friday" — as it does in cron, croniter, and every scheduler
  users compare against — where AgentOS previously required a date to be both a
  1st/15th *and* a Friday, silently killing such schedules for virtually the
  whole month. `CronField` now records whether the field was written as a bare
  `*`, since expanding `*` to the full value set made it indistinguishable from
  an explicit `1-31`/`0-6` at match time and the rule applies only when neither
  day field is a wildcard. Schedules with a wildcard in either day field are
  unchanged. This also restores parity with the cron panel in the web UI, whose
  "next runs" preview (`frontend/src/views/cron/logic.ts`) has always applied
  the OR rule — so the times it showed disagreed with when the job actually
  fired. ([#660](https://github.com/use-agent-os/agent-os/issues/660))
- `MemorySyncManager` retries a file whose indexing failed instead of losing it
  until the next edit. `_do_file_sync()` replaced `_mtimes` with the fresh scan
  *before* the index loop ran, so by the time `store.index_file()` raised, the
  failing path was already recorded as seen — the next watcher tick compared
  equal, the path never entered `changes`, and the retry its docstring promised
  never happened. A transient store error (SQLite lock, provider timeout) on
  `MEMORY.md` therefore left searches running against a stale or missing index
  for that file until it was modified again or the process restarted. Index
  failures now come back from `_do_file_sync()` alongside the existing delete
  failures and are re-enqueued into `_pending_changes`, keeping the manager
  dirty until a retry succeeds. The initial `start()` pass re-enqueues too,
  where `_mtimes` is empty and the watcher diff could never have recovered the
  path (#638).

- `OtlpTraceSink.flush()` is serialized by the `_flush_lock` it always
  declared but never acquired. Concurrent flushes — a `write()` batch trigger
  racing the periodic flush task — could post to the OTLP collector
  simultaneously, delivering spans out of order and, when a post failed,
  re-queueing the same events twice so they were duplicated in the queue. The
  lock is now held across the drain-post-requeue cycle, with an empty-queue
  fast path before it so the uncontended case stays allocation-free
  ([#672](https://github.com/use-agent-os/agent-os/issues/672)).
- `agentos sessions export` derives its default filename through
  `_safe_archive_part` instead of only replacing `:`. A session id is
  gateway-supplied text, and every character outside `[A-Za-z0-9_.-]` — a `/`
  or a `..` segment among them — reached `Path()` untouched, so the export
  could be written outside the directory the command was run in. The shared
  helper also now strips leading and trailing dots, so an id that sanitizes to
  `..` can no longer name the parent directory
  ([#678](https://github.com/use-agent-os/agent-os/issues/678)).
- HTTP chat errors name the provider that actually failed.
  `_provider_display_name` mapped only a handful of kinds, so Azure, Bailian,
  Mistral, Groq, SiliconFlow, AIHubMix, MiniMax, BytePlus, Bankr, vLLM,
  LM Studio and OVMS all surfaced as a generic "Provider" in the message the
  user reads.

### Security

- The strict SSRF fetch guard now enforces the cloud-metadata floor directly
  instead of inferring it from the private/link-local ranges. `ssrf.py` keeps a
  shared `_METADATA_ADDRESSES` set described as the non-negotiable floor, but
  only the permissive guard (`assert_not_metadata_endpoint`, used by
  `http_request`) consulted it. The stricter `assert_address_allowed_for_fetch`
  — used by `web_fetch`, the media image fetch, browser navigation and
  skill-dependency downloads — derived its coverage from `is_private` /
  `is_loopback` / `is_link_local` / `is_reserved` instead.

  That left the two guards inverted for one address. Alibaba Cloud's
  `100.100.100.200` sits in CGNAT space (`100.64.0.0/10`), which Python
  classifies as none of those and which no hard-blocked network covers, so the
  *strict* guard allowed it while the *permissive* one blocked it. On an
  Alibaba ECS deployment a URL the agent could be steered to fetch — directly,
  or by prompt injection from page content it reads — returned the instance RAM
  role credentials into the transcript. The connect-time guard shares the same
  predicate, so DNS-delivered and redirect-hop variants were equally unguarded.

  The metadata hostname check (`metadata.google.internal` and friends) now runs
  in `validate_http_url_for_fetch` too, so a resolver answering those names
  cannot launder the request through a public-looking address. Fetch policy is
  a strict superset of the metadata-only policy again, and a parametrized test
  asserts that for every entry in the shared set — the invariant that was
  missing, rather than the single address that happened to break it.

- The MCP SSE and Streamable HTTP transports now connect through the same
  SSRF guard as the built-in HTTP tools. Both built a bare `httpx.AsyncClient`
  from `MCPServerConfig.url` with no validation at all, so an MCP server entry
  pointed at `169.254.169.254` reached the cloud metadata endpoint and its
  instance credentials.

  The policy is `validate_metadata_only_address` — the floor `http_request`
  takes — not the full `validate_http_url_for_fetch`: `http://localhost:PORT/mcp`
  and LAN-hosted MCP servers are the normal, intended configuration, and the
  stricter policy rejects loopback and private ranges. The guard is installed as
  a connect-time network backend (`ssrf_guarded_client`) rather than run once
  against the URL text, so the address that gets validated is the address that
  gets dialed: checking the URL and then handing it to a plain client leaves
  httpx to resolve the hostname a second time, which a short-TTL DNS-rebinding
  name can answer differently. Non-`http(s)` server URLs are now rejected up
  front. ([#662](https://github.com/use-agent-os/agent-os/issues/662))

- Slack webhooks are rejected when no signing secret is configured, instead of
  being ingested. `_handle_webhook` logged a warning and carried on:
  `event_callback` payloads were ingested and slash commands were enqueued, so
  any unauthenticated POST to the Events API endpoint could inject messages and
  commands into a session — only interactive form payloads were turned away.
  The handler now fails closed. Without a signing secret it still answers the
  `url_verification` handshake — that only echoes a challenge and has no side
  effects, so an operator can pass Slack's endpoint check while wiring the
  secret up — and returns 401 for everything else
  ([#674](https://github.com/use-agent-os/agent-os/issues/674)).
- Slack request signatures are verified against the raw request bytes. The
  base string was assembled as text (`f"v0:{timestamp}:{body.decode()}"`) and
  re-encoded, so any body whose bytes do not survive a UTF-8 decode/encode
  round-trip — and any body that fails to decode at all, which raises inside
  the verifier — was checked against a different byte sequence than the one
  Slack signed. The HMAC is now computed over `b"v0:" + timestamp + b":" +
  body` with the body never decoded
  ([#680](https://github.com/use-agent-os/agent-os/issues/680)).

## [2026.9.2] - 2026-09-02

### Added

- **Surplus Intelligence** (`surplus`) as a runtime provider — a two-sided
  marketplace that routes each request to the cheapest healthy seller. It is
  configured like any other OpenAI-compatible provider with a buyer API key
  (`SURPLUS_API_KEY`, `inf_…`) against
  `https://api.surplusintelligence.ai/v1`; the x402/USDC and MPP per-request
  payment protocols it also offers are deliberately not wired up, so nothing
  crypto-related enters the dependency tree.

  Its model catalog is public and unauthenticated, and follows OpenRouter's
  shape rather than the flatter gateway one — rates are USD *per token*, and an
  extra `supported_features` array names `vision`/`reasoning`/`tools` directly.
  Because marketplace prices move with seller competition, cost estimates come
  from that live catalog instead of a static table: the boot fetch doubles as
  the price seed and refreshes on its own TTL, with a bounded negative cache
  when it is unreachable. `AGENTOS_SURPLUS_LIVE_PRICING=0` pins estimates to
  the static table.

  Ships a `surplus` router tier profile (`deepseek-v4-flash`, `gpt-5.6-luna`,
  `glm-5.3`, `claude-opus-5`, image `glm-5.3-flash`). Without one the router
  would silently fall back to the OpenRouter tier table, whose namespaced ids
  (`openai/gpt-5.6-luna`) this marketplace does not serve. The image tier is
  `glm-5.3-flash` rather than OpenCAP's `minimax-m3`: Surplus publishes
  `minimax-m3` without vision.

- Two GMGN wallet skills the earlier vendoring pass left behind:
  `gmgn-wallet-analysis` (a copy-trade dossier on one wallet — four pass/fail
  gates, what it holds and buys now, its copy window in seconds, and a size cap)
  and `gmgn-wallet-score` (track record, copy-tradeability with a
  latency/slippage/gas backtest, and developer reputation for wallets that
  mostly launch tokens). Both answer "should I follow this trader", which the
  bundled set could previously only support with raw `gmgn-portfolio` fields.
  Upstream's `gmgn-wallet-score` frontmatter is not valid YAML — an unquoted
  `: ` inside `description` — so its description is folded into a block scalar
  here; without that the loader drops the skill silently.
- New bundled skill `robinhood-chain-stocks`: reads tokenized-stock state
  directly from Robinhood Chain (chainId 4663) over JSON-RPC — Chainlink USD
  price, the ERC-8056 `uiMultiplier()` corporate-action ratio, `oraclePaused()`,
  total supply, and wallet balances with their USD value. Read-only by
  construction: it issues only `eth_call` and never signs, sends, or holds a
  key. Feed addresses are resolved from Chainlink's reference-data directory
  rather than hardcoded, resolved from the ticker the contract reports so an
  address-only lookup still finds its price. Prices carry `ageSeconds` and a
  `stale` flag (past the feed's heartbeat, or oracle paused) so a market-closed
  quote is not read as the current price, and a non-positive feed answer is
  reported as unusable instead of `$0`. Authenticity is reported three ways —
  verified, disproven by a revert, or unverified because the node was
  unreachable — so a network fault is never presented as proof that a genuine
  listing is fake.

- `agentos cost savings` reports what the Pilot Router actually saved. Every
  turn already wrote `SavingsTelemetry` to `~/.agentos/logs/decisions-*.jsonl`
  and nothing read it back; the existing reports covered routing quality and
  feature-extraction latency, not dollars. The command rolls that telemetry up
  into a summary and a per-route breakdown with `--json`, `--csv`,
  `--start-date` / `--end-date`, `--log-dir`, and `--pdf` for a branded
  one-page report. It reads the decision log directly, so it works with the
  gateway stopped.

  The figure is a floor, and the report says so on the page. Despite its name,
  `routing_savings_usd_estimated_vs_baseline` is not measured against the
  sibling `baseline_model` field: it is the input-price delta between the
  routed model and the most expensive model configured in `[router.tiers]`,
  times input tokens, clamped at zero. So the column is labelled `Requested`,
  the comparison is named as the top tier, and only input tokens are priced —
  tool-result projection, short-reply enforcement, prompt-cache hits and
  thinking mode are all excluded so the number stays attributable to the
  router (#788).

### Changed

- The default skills-prompt budget (`skills.max_skills_prompt_chars`) rises from
  24,000 to 26,000 characters. The shipped skill set's own descriptions had grown
  past the old ceiling, which would have dropped full-mode installs to a
  narrower render; the budget is a cap, so installs that were already under it
  send no more than before. Configs that set the value themselves are untouched.
- OpenCAP's router tier defaults track OpenCAP's own catalog again. `c2` moves
  from `glm-5.2` to `glm-5.3` (1.31M context, published by the gateway since the
  last update), and the profile is declared in its own table instead of being
  cloned from the Bankr profile with the provider string swapped — the two
  gateways publish overlapping but different catalogs, so cloning made OpenCAP
  silently inherit Bankr's release cadence. `c0` (`deepseek-v4-flash`), `c1`
  (`gpt-5.6-luna`), `c3` (`claude-opus-5`) and the image route (`minimax-m3`)
  are unchanged; each is still the newest of its family the gateway serves.
- Five models OpenCAP now publishes are declared in the model registry:
  `glm-5.3`, `glm-5.3-flash`, `grok-4.6`, `kimi-k3` and `muse-spark-1.2`. They
  carry published context windows, output caps and vendor rack rates, so an
  offline estimate for them no longer falls through to the generic $3/$15
  default. OpenCAP's live catalog remains canonical for its own pricing.
- The Ollama "model not found" branch of `classify_provider_error` now spells
  out its grouping as `"model not found" in text or ("pull" in text and "model"
  in text)`. The behaviour is unchanged — `and` already bound tighter than `or`
  — but the intent no longer rests on implicit precedence, and the branch is
  now covered by tests (#582).

### Fixed

- The email channel no longer honours an off-allowlist `Reply-To`. The From
  address was checked against the fail-closed `allowed_senders` list, but the
  `Reply-To` header — equally attacker-controlled on an admitted message — was
  taken verbatim as the reply target, so an allowlisted sender could redirect
  the agent's answer, tool output included, to any mailbox. `Reply-To` is now
  run through the same allowlist and falls back to the From address when it is
  off-list, rather than the whole message being rejected. The reply target is
  re-checked when the outbound reply is built, so a stale or tampered thread
  cache cannot reintroduce an off-list recipient.
- A `thinking_level` set on an OpenCAP GLM tier is no longer silently dropped.
  The gateway capability gate reported `supports_reasoning=False` for every
  model except DeepSeek V4, so the `c2` default's declared `thinking_level`
  never reached the wire even though GLM 5.x reasons by default and streams
  `reasoning_content`. GLM ids on OpenCAP now resolve Z.ai's
  `{"thinking": {"type": ...}}` switch, verified live in both positions. Scoped
  to OpenCAP; the Bankr gateway is a separate deployment and keeps its previous
  behavior.
- The offline vision fallback recognizes `gpt-5.6-*`, `glm-5.3-flash` and
  `muse-spark-*` as image-capable. Previously, if the catalog fetch failed, the
  `c1` default was reported as text-only and image turns had nowhere to route.
- `upsert_llm_provider` now validates an operator-supplied provider `base_url`
  before it is persisted or handed to the httpx client. The RPC
  (`onboarding.provider.configure`) and `agentos providers configure` accepted
  any string, so a caller could point every completion request — carrying the
  provider `Authorization` header — at a cloud metadata service, an internal
  host, or an attacker's server, or hand a `file://` URL to httpx. The value
  must now be an absolute http(s) URL and may not be a cloud metadata endpoint
  or a private / link-local / reserved IP — including the `inet_aton` spellings
  (`http://2852039166/`) that reach the metadata service without looking like
  an address. Loopback stays allowed for local model servers, and a `base_url`
  that is already persisted (a saved profile, a provider default, or the value
  the onboarding import path replays) is not re-validated (#551).

- `robinhood-rwa-addresses` no longer answers a company question with a
  community token that impersonates it. Robinhood Chain is permissionless and
  the public token list carries both kinds: two entries are named "GameStop"
  with symbol `GME`, and the lookup stripped the `• Robinhood Token` suffix —
  the only thing telling them apart — before ranking, so which address came
  back was down to list order. Asking for `NET` returned the "NetNet" community
  token above Cloudflare. The lookup now matches Stock Tokens only (opt back in
  with `--include-community`, where real listings still rank first), tags every
  match with `isStockToken`, and reports a `stock_tokens` count. The skill doc's
  hardcoded "~228 tokens" claim, stale against the 658-entry list, is gone
  (#745).
- Email channel outbound sends no longer fail for every agent-initiated
  message. `EmailChannel._resolve_target` read only `metadata["to"]` and the
  in-memory inbound-thread table, so the built-in `message` tool (which writes
  the target as `metadata["recipient"]`) and scheduler / heartbeat delivery
  (which pass the bare address as `reply_to`) both raised
  `ValueError: email.send has no recipient for reply_to`. Recipients now
  resolve in order: `metadata["to"]`, `metadata["recipient"]`, the thread
  cache, then `reply_to` when it parses as an address; a fresh outbound mail
  with no thread also gets a real subject instead of `Re: (no subject)` (#598).
- **Security (SSRF, DNS rebinding):** the SSRF guard validated a URL by
  resolving its hostname once, but httpx resolved that hostname *again* when it
  opened the connection — so a short-TTL (rebinding) domain could answer with a
  public address for the guard and with `169.254.169.254` for the socket,
  handing an agent the cloud metadata endpoint and the instance credentials it
  serves. `agentos.tools.ssrf_client` adds a validating httpcore network backend
  that resolves and checks the destination itself at connect time and then
  connects to a validated IP literal, so the address that was checked is the
  address that is used; TLS is unchanged (SNI and certificate verification still
  run against the origin hostname). `web_fetch`, the media image fetch,
  `http_request` and `x_search` (metadata-endpoint floor only, so localhost and
  LAN targets keep working) and skill-dependency downloads all fetch through it
  (#516).
- Gemini context-overflow errors classify as `CONTEXT_OVERFLOW` again. Gemini
  reports overflow as `the input token count (X) exceeds the maximum number of
  tokens allowed (Y)`, which no marker matched, so it fell through to the
  `status_code == 400` branch and surfaced as `BAD_REQUEST` — the turn died
  instead of taking the `COMPACT_AND_RETRY` path (#657).
- Anthropic context-overflow errors do the same. `prompt_too_long`,
  `exceed context limit`, `request_too_large` and `request size exceeds` join
  the marker list, so an overflowed Anthropic turn compacts and retries rather
  than surfacing a bad-request error to the user (#613).
- The `@sandboxed` decorator derives a valid argv for `git_diff`. The
  `argv_factory` produced a command line the sandbox could not run, so the
  tool failed under sandboxing rather than being inspected and allowed (#614).
- `web_fetch` decodes the body with the charset the server advertised
  (`Content-Type`'s `charset` parameter) instead of a hard-coded UTF-8 decode,
  so ISO-8859-1, Shift_JIS and GBK pages no longer reach the model as runs of
  U+FFFD. The encoding is snapshotted inside the client block before any body
  read, matching what `http_request` already does.
- `MemorySyncManager` passes the filesystem mtime its watcher already captured
  to `LongTermMemoryStore.index_file()`, for both watched memory files and
  knowledge-base documents. Persisted freshness and retrieval recency now
  track the source file rather than the moment the sync ran, matching the
  direct ingestion path — and without an extra stat (#649).
- The scheduler cancels its startup catch-up tasks on shutdown. They were
  fired and never retained, so a shutdown mid-catch-up left them running
  against a closing runtime instead of being cancelled and awaited with the
  regular timer tasks (#655).

### Security

- `GET /api/approvals` is no longer exempt from per-IP rate limiting. The
  endpoint serializes every pending exec/plugin approval — command, argv and
  params — and takes a SQLite read on each call, so the carve-out let any
  caller that clears the auth gate poll it at unlimited rate: continuous
  observation of pending tool-call arguments, and enough SQLite read pressure
  to stall the approval/chat pipeline. On the default `auth.mode="none"`
  (loopback-confined) that is any local process; under token auth it is any
  token holder. `HEAD` is covered too — Starlette serves it from the same
  route, so it runs the same handler.

  It is now counted in a dedicated per-IP bucket rather than the shared
  `/api/*` one, because the Web UI polls it every 1.5s (~40 req/min per open
  tab) and the shared default of 100/min would have 429'd operators out of
  their own approval queue. The cap is `AGENTOS_RATE_APPROVALS_MAX_REQUESTS`,
  default 300 per window. Being per-IP, it bounds a single source; it is not a
  defence against a distributed one (#569).
- Proxy names are no longer writable through any AgentOS surface. `set_env_var`
  (and the Web UI, `agentos env set`, and the gateway RPC) could previously
  write `AGENTOS_LLM_PROXY`, which `gateway/llm_runtime.py`, `provider/openai.py`
  and `provider/auxiliary.py` apply to every provider client — letting an agent,
  or a prompt injection reaching one, route all model traffic through a proxy of
  its choosing and read the `Authorization` header off it. `AGENTOS_LLM_PROXY`,
  `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` now join
  `env_policy.WRITE_DENYLIST`, matched in **any casing** — the proxy readers
  (`urllib.request.getproxies_environment()`, which httpx goes through)
  lower-case every name they find, so denying only the two conventional
  spellings would leave `Http_Proxy` as an equivalent way in. `AGENTOS_TRUST_ENV`,
  which decides whether the ambient `*_PROXY` names are honoured at all, is
  denied with the other posture names. Values exported in the shell or
  hand-written into `~/.agentos/.env` keep working; only writing through
  AgentOS is refused (#550).
- **Trusted-proxy auth validates the transport peer, not a header substring.**
  `auth.mode = "trusted-proxy"` admitted any request whose client-supplied
  `X-Forwarded-For` merely *contained* the configured proxy string — the real
  peer was never checked, so any network peer could send
  `X-Forwarded-For: <proxy>` and get full Control RPC access. The gate now
  requires `request.client.host` to be in the trusted-proxy set; once that
  passes, `XFF` is honoured downstream for client identity, which is what the
  mode exists for (nginx / Caddy / ALB all set it). The trust check is a single
  shared `peer_is_trusted_proxy` helper used by `AuthMiddleware`,
  `RateLimitMiddleware` and the RPC `resolve_auth` layer — which previously had
  no trusted-proxy branch at all — so the two gates cannot drift (#568).
- **Cron webhooks cannot reach cloud metadata endpoints.**
  `validate_webhook_url` checked only scheme and hostname, so a cron job could
  POST its run output — model output and tool results — to AWS IMDS, the GCP or
  Azure metadata service, or anything else in the link-local range, and the
  response would come back as the delivery result. The shared metadata floor
  `http_request` already uses is now applied on create, on update, and at
  delivery time. Localhost hooks keep working (#574).
- **`web_fetch` downloads are capped at a hard byte limit.** The whole response
  body was buffered into memory before `max_chars` was applied — `max_chars`
  truncates what the model sees, not what is downloaded — so one chunked
  response with no (or a lying) `content-length` could exhaust process memory
  and take the agent or gateway down; the 30s timeout bounds time, not bytes.
  The response is now streamed and reading stops at
  `AGENTOS_WEB_FETCH_DOWNLOAD_LIMIT` (default 1 MiB), with `truncated=True`
  when the cap is hit. The response is closed only after the redirect
  `Location` header is read, so a 3xx with no `Location` no longer fails
  against a closed stream (#502).
- **The exec approval cache parses every `rm` in a compound command.** It used
  `re.search`, which stops at the first match, so `rm A; rm -rf /` had its
  second invocation skipped entirely — the destructive target never reached the
  intent scan. Every `rm` is now tokenized independently with `re.finditer`,
  with capture stopping at shell separators. Regression tests pin both layers:
  every separator ends an `rm` invocation, and a sensitive *read* in a later
  segment is still refused at the tool boundary by `exec_command`'s
  whole-command scan (#512, #676).

## [2026.9.1] - 2026-09-01

### Added

- Observability for long-running gateways: a Prometheus `GET /metrics` endpoint
  backed by thread-safe multi-dimensional `Counter` / `Gauge` / `Histogram`
  types wired to `TaskRuntime`, an `OtlpTraceSink` that exports `TraceEvent`
  records to any OpenTelemetry collector over HTTP/JSON (`/v1/traces`), and a
  log-retention sweeper that prunes `~/.agentos/logs/**` by TTL age and by a
  maximum total disk budget so a gateway left running for months no longer
  fills the disk (#367).

### Changed

- Project knowledge is now capped at 24,000 characters on write — the same
  ceiling the per-turn injection applies — instead of 32,000. Text between the
  two caps used to save fine, echo back intact from the API, and then be
  silently truncated out of every turn with only the model able to see the
  marker. Rows already above the new cap keep working (validated on the next
  write, truncated at injection until then), and the Web UI knowledge editor
  now shows a character counter for the real limit.

### Security

- The `projects_*` agent tools are now scoped to the calling session.
  `projects_list` used to hand the model every project's knowledge text and
  `projects_update` accepted any `project_id`, so one prompt-injected
  instruction in any member session could read all knowledge and overwrite
  another project's — text that then runs inside the system prompt of every
  member session of that project, every turn. `projects_move_session` likewise
  accepted arbitrary session keys, allowing the same hand-off by moving a
  victim session into a poisoned project. Now `projects_update` edits only the
  calling session's own project, `projects_list` includes knowledge only for
  that project, and `projects_move_session` moves only the calling session;
  cross-project management stays on the Web UI / CLI / RPC surface, which is
  control-plane only.
- Gateway token authentication now compares secrets in constant time. The four
  token gates (`resolve_auth` for WebSocket/RPC, the HTTP `AuthMiddleware`, the
  upload route and the audio-transcription route) used `==`/`!=`, which
  short-circuits on the first differing byte and leaks the token byte by byte
  under timing analysis. All four route through a shared `token_matches` helper
  built on `hmac.compare_digest` that fails closed on a missing or empty
  configured token; the auth contract is otherwise unchanged (#498).
- The sensitive-payload egress guard now inspects URL userinfo. It scanned path
  segments and query values only, but httpx turns `https://user:sk-…@host/` into
  an `Authorization: Basic` header on the wire, so a vendor-shaped credential
  parked in userinfo egressed unchecked through `http_request`, `web_fetch`
  (on every redirect hop) and the media `image` tool. Username and password are
  now percent-decoded and matched, raising a `sensitive_url_userinfo` marker
  (#499).
- `http_request` now caps what it downloads, not just what it returns. The
  request was issued non-streaming, so httpx buffered the whole body into
  memory before the 1 MB model-facing limit was applied — a chunked response
  with no (or a lying) `content-length` read fully into RAM, letting one
  attacker-influenced URL exhaust process memory. The response is now streamed
  and accumulation stops at a hard byte ceiling, reporting `download_capped`
  (#508).
- Scheduler `timeout_seconds` is bounded on both cron create and update.
  It was accepted unvalidated: `<= 0` makes `asyncio.wait_for` run the handler
  with no wait at all, and a huge value holds a model turn open for years — a
  scheduler denial of service from a single `add`/`update` call. Values below
  1 second or above 24 hours are now rejected (#570).

### Fixed

- `robinhood-rwa-addresses` no longer answers a company question with a
  community token that impersonates it. Robinhood Chain is permissionless and
  the public token list carries both kinds: two entries are named "GameStop"
  with symbol `GME`, and the lookup stripped the `• Robinhood Token` suffix —
  the only thing telling them apart — before ranking, so which address came
  back was down to list order. Asking for `NET` returned the "NetNet" community
  token above Cloudflare. The lookup now matches Stock Tokens only (opt back in
  with `--include-community`, where real listings still rank first), tags every
  match with `isStockToken`, and reports a `stock_tokens` count. The skill doc's
  hardcoded "~228 tokens" claim, stale against the 658-entry list, is gone.

- Two clients editing the same project no longer overwrite each other.
  `projects.update` used to read the whole row, apply the change, and write
  every column back, so a rename holding a stale row silently reverted a
  concurrent knowledge save. Updates now write only the fields passed, and the
  Web UI sends the `updatedAt` it last read so a lost race returns a
  `project.conflict` error (draft kept, latest version loaded) instead of
  clobbering. Same-millisecond writes get distinct `updated_at` values, and a
  unique index on project names (V012) backstops the duplicate-name check
  under concurrent creates.
- The Projects page now listens to the gateway's `projects.changed` /
  `sessions.changed` broadcasts, so another client's create, rename, delete,
  or session move shows up without pressing Refresh. Moving a session between
  projects via `sessions.patch` also broadcasts `projects.changed`, keeping
  other clients' session counts fresh, and the move can no longer be reverted
  by a simultaneous field patch on storage-only session managers.
- The Projects page no longer renders its loading and error states as the
  "No projects yet" empty state (with a create button) — loads show a spinner
  and failures show the error with a Retry action. Browser back/forward can no
  longer leak one project's unsaved draft into another project's editor, and
  a saved knowledge edit no longer flashes the stale pre-save text.

- Router metadata no longer reports a model the provider never ran. An explicit
  model — a durable `config.agents[].model`, a session pin, or a per-call
  override — beats the Pilot Router's pick when `PromptAssemblerStage` resolves
  the final model, but the metadata kept advertising the route as applied, so
  the Web UI router HUD, the `DoneEvent`, per-turn usage and the savings figures
  all named the routed model and credited savings for a route the turn never
  took. The decision is now demoted the same way the `observe` rollout phase
  already does it (`routing_applied=false`, tier and model kept on the record as
  advice), and per-turn savings are priced from the model that actually ran —
  which also corrects the cost basis reported during `observe` (#586).
- Two provider failures that made whole model families unusable. Requests to
  the `opencap` and `bankr` provider kinds now carry the required `x-api-key`
  header on chat completions and model listing, instead of failing with
  `HTTP 401: API key required for remote API access`. And Gemini reasoning
  models no longer reject tool calls with `HTTP 400: Function call is missing a
  thought_signature`: the signature is captured from streamed and non-streamed
  deltas, carried on `ToolUseEndEvent` / `ContentBlockToolUse` / `ToolCall`
  through the turn loop, session sanitization and history deserialization, and
  echoed back when messages are rebuilt (#519).
- `agentos upgrade` no longer leaves orphaned processes on Windows. On a
  timeout, `_kill_process_group()` called `proc.kill()`, which terminates only
  the direct child — grandchildren (compilers, downloads, nested Python runs)
  survived and kept file locks on the virtualenv. Windows now uses
  `taskkill /T /F /PID` to kill the whole tree, falling back to `proc.kill()`
  only if that fails; POSIX still uses `os.killpg` SIGTERM→SIGKILL (#536).

## [2026.8.29] - 2026-08-29

### Added

- Memory Web UI view and knowledge-base document ingestion. The console gets a
  browsable `/memory` view (sidebar entry, `g m` chord) with a curated-memory
  editor, a knowledge-base document table, a raw source-file explorer and a
  semantic search explorer. Behind it, `memory.ingest` grows multi-format text
  extraction and directory ingestion (PDF, DOCX, PPTX, Markdown, text, CSV,
  JSON/YAML and code files) over `<workspace>/knowledge_base/`, exposed as
  `memory.curated.*` and `memory.knowledge_base.*` JSON-RPC methods and as
  `agentos memory ingest` / `agentos memory curated` on the CLI (#368).

- Email channel (`type = "email"`). A mailbox is now a first-class channel:
  inbound over IMAP polling, outbound over SMTP with `In-Reply-To`/`References`
  so replies stay in the originating thread. No platform app registration —
  just IMAP/SMTP credentials. One mail thread is one session, quoted history is
  stripped before the text reaches the model, HTML-only mail is flattened to
  text, and inbound attachments plus generated artifacts ride the shared
  attachment pipeline under the usual size limits. Access is a required
  fail-closed `allowed_senders` From-address allowlist (exact addresses or
  `*@domain` patterns); mail from the agent's own address and anything marked
  auto-generated (`Auto-Submitted`, `X-Autoreply`, `List-Id`,
  `Precedence: bulk`) is dropped so an autoresponder cannot start a mail loop
  (#369).

### Changed

- Channel session keys: a DM-shaped channel whose surface is itself threaded
  can opt into one session per thread with `metadata['dm_thread_scoped']`.
  Adapters that do not set it keep one session per peer, so Slack, Discord and
  Telegram DM keys are unchanged.

### Fixed

- Security: the git tools (`git_status`, `git_diff`, `git_log`, `git_commit`)
  now mask credentials in their output before it reaches the model. `git_diff`
  returns working-tree and staged file content verbatim, so a `.env` that was
  committed once kept reaching the model in cleartext on every diff, while the
  sibling file surfaces (`read_file`, `grep_search`) already redacted. Masking
  happens at the one `_run_git` chokepoint, on the sandboxed and subprocess
  paths and on success and failure alike. The assignment pass runs
  unconditionally (`code_file=False`) because a diff is arbitrary repository
  content and the git argv says nothing about what is coming back, and the
  non-reusable `«redacted:…»` sentinel is used because an agent may pipe a diff
  straight back through `git apply`.
- Security: a named credential on a diff line no longer escapes the assignment
  redaction pass. The token-start anchor did not admit the diff marker, so
  `+MY_SECRET=…` went unmasked where `MY_SECRET=…` was masked; vendor-prefixed
  keys were still caught by the shape pass, non-vendor named secrets were not.
  The anchor now accepts one or two marker columns, covering the combined diff
  a conflicted merge produces as well as the ordinary unified form.
- Slack sends and scheduler webhook deliveries now survive a transient network
  blip. Both routed their HTTP calls straight at `httpx` and failed on the
  first error; they now go through the same `retry_request` helper Discord
  already uses (exponential backoff with jitter on 429 — honouring
  `Retry-After` — 500/502/503/504, connect errors, and read timeouts). Fatal
  statuses such as 400/401 still fail on the first attempt, and the webhook
  retry keeps the stock 3-retry/1s-base budget so its worst case stays inside
  the cron job's own timeout. Note that retrying a read timeout on
  `chat.postMessage` or a webhook POST can duplicate a delivery the receiver
  already accepted — the same trade-off Discord has always made; the webhook
  payload's `jobId` is the receiver's dedupe key.

- Security: `execute_code` output is redacted before it reaches the model.
  `shell.py` already ran `redact_terminal_output` on every output surface, but
  `execute_code` bypassed redaction entirely, so a script printing `os.environ`
  or reading a credential file leaked every secret verbatim into the
  transcript. Redaction now happens at `_execution_result_json`, the single
  choke point for all eight return paths (#490).
- Security: the `image` tool no longer buffers an unbounded response body
  before checking its size limit. `_fetch_image_url` read `resp.content` in
  full and only then compared against the 20 MB ceiling, so an oversized or
  chunked body could exhaust process memory. The response is now streamed and
  the read stops the moment the accumulated size passes the limit; each
  redirect hop is still SSRF-checked before its body is read (#506).
- Security: the GitHub skill-hub source caps blob downloads. `GitHubSource.fetch()`
  buffered every blob of a skill directory with a non-streaming `client.get`,
  with no per-blob cap and no total budget, so a hostile repo could push the
  installer into RAM exhaustion. Blobs are now streamed against a per-blob
  ceiling (8 MiB) and a cumulative budget (32 MiB), the response is closed in a
  `finally`, and a blob over the cap fails closed rather than installing a
  truncated bundle (#510).
- Scheduler: one-shot `AT` schedules in the past are rejected by
  `SchedulerOps.add()` and `update()` (5s skew tolerance) instead of being
  stored with a stale `next_run_at` that fires on the very next tick (#486).
- Scheduler: `next_due_at` now reports the actual runnable time,
  `MIN(MAX(next_run_at, backoff_until))`. It previously looked only at
  `next_run_at` while `iter_due` also waits on `backoff_until`, so after a few
  failures on a frequent cron the timer woke early, yielded nothing and
  busy-spun SQLite for the whole backoff window (#537).
- MCP stdio: the live reader uses `readexactly` instead of `read(n)`, which
  could return a short buffer and truncate a chunked tool result into a
  `json.loads` failure; EOF now raises a clear truncated-body error (#537).
- Telegram: entity offsets are sliced on the UTF-16 grid. Offsets and lengths
  are UTF-16 code units but were applied to a Python `str` by code point, so an
  emoji before `/help@mybot` in a group made the bot ignore a command aimed at
  it (#537).
- Provider failover: an exhausted fallback chain raises the explicit
  `IndexError("No more provider fallbacks available")`.
  `next_fallback_after_failure()` advanced the chain index unbounded and
  surfaced a bare out-of-range `IndexError` from `_build_provider` (#488).
- Discord: `_dispatch_loop` keeps dispatching after a reconnect. Opcode 7/9 and
  a dropped socket reconnected and then returned from the loop — heartbeat
  resumed and health still read connected, but messages and slash commands were
  never read again (#538).
- Setup: cancelling xAI sign-in stops the poll loop. Cancel only reset the
  visible card, so an expiry could paint an error after dismissal and a
  restarted sign-in could be wiped by the old loop completing; cancel and start
  now bump a generation counter (#538).
- Workspace paths: a real nested `workspace/` folder inside the configured root
  is no longer stripped. Any absolute path containing a `workspace` segment was
  rewritten from the last such segment, so reads and writes landed on a sibling
  file; paths already inside the root are left alone and sandbox
  `/workspace/...` still remaps (#538).
- Web UI: the projects page header stacks on mobile instead of overflowing.

## [2026.8.28] - 2026-08-28

### Added

- Projects. Chat sessions can now be grouped into projects — cross-agent, so
  sessions of any agent can join the same project (a project's agent field is
  only the default for "new chat in project") — each carrying a free-form
  **knowledge** text that is injected into the system prompt of every member
  session (as an untrusted-wrapped `Project Knowledge`
  block, re-read each turn so edits land on the next turn). Surfaces: a new
  Projects page in the Web UI (create/rename/edit knowledge/delete, "New chat
  in project", session list per project), project badge + filter + "Move to
  project" on the Sessions page, project tiers in the chat session switcher,
  an `agentos projects` CLI group (`list`/`create`/`show`/`update`/`delete`/
  `move`), `projects.*` JSON-RPC methods plus `projectId` on
  `sessions.create`/`sessions.patch`/`sessions.list`, and agent-facing
  `projects_*` tools with `session_search scope=project` for searching sibling
  transcripts. Existing databases migrate automatically (V011); old sessions
  come up project-less, and deleting a project detaches its sessions instead
  of deleting them.

### Fixed

- Cron: day-of-week `7` is Sunday again, so `0 0 * * 7` schedules Sundays
  instead of being rejected (#478).
- Cron: a reversed range in a stepped field (`30-20/5`) is refused up front
  with a clear error instead of parsing into surprise fire times (#480).
- Cron: month and day-of-week names are case-insensitive — `jan`, `JAN` and
  `Jan` are the same month, `sun`/`SUN` the same day (#482).
- Scheduler: the timezone alias on a legacy expression schedule is honored
  instead of silently falling back (#485).

## [2026.8.27] - 2026-08-27

### Added

- Spend budgets. A new `[budgets]` config section sets money ceilings per
  session, per UTC day, per agent, and per channel. A turn that starts at or
  above a hard limit is refused before any provider call with a
  `budget_exceeded` error naming the scope and the number; a matching
  `*_warn` threshold raises a one-shot `budget_warning` without stopping the
  turn. Ceilings are re-checked between iterations within a turn as well, so a
  single turn with a long tool loop cannot run past one. Spend is persisted to
  `~/.agentos/state/spend_ledger.db`, so a ceiling survives a gateway restart —
  a runaway overnight loop cannot be reset by a crash-and-respawn. Nothing is
  enforced until an operator sets a number.

- `aero-stock-lp` joins the Bankr skill hub. The skill range-LPs Coinbase
  tokenized equities (NVDA, AAPL, GOOGL, META) and AERO/USDC on Aerodrome
  Slipstream (Base) — opening, recentering, and exiting concentrated-liquidity
  positions, reporting pool status, NAV, yields, and P&L, and routing each
  position to whichever side pays more at this epoch, staked for AERO emissions
  or unstaked for trading fees. It is published as a directory in
  `BankrBot/skills`, so it browses and installs through the existing repo half
  of the Bankr source with no new code path.

### Removed

- Three subsystems that shipped in the wheel while being dead or
  permanently-failing are gone (#362). The `onboard_agent` wizard — the
  `wizard.start` / `wizard.next` / `wizard.cancel` / `wizard.status` RPC
  methods plus their state machine — had no caller in the frontend or the CLI
  and no side effect: its terminal step returned the collected answers and
  never created an agent, while its hardcoded model list had gone two
  generations stale. The Agents view already creates agents through
  `agents.create`. The `canvas` and `nodes` built-in tools validated their
  `action` argument and then raised `ToolError` unconditionally; no node
  runtime exists anywhere in the tree to configure, and only
  `exposed_by_default=False` kept them from failing in front of a model.
  `tools/visibility.py` no longer exports `filter_by_profile` (returned its
  input) or `profile_allows_tool` (returned `True`), nor does the dispatch
  chain run the `ProfilePolicy` that only called them — profile enforcement
  now has one home in `tools/policy_config.py`. `ToolProfile` and
  `resolve_profile` stay; they are the live seam.

- The `agentos dist` install inventory no longer advertises built-in tools that
  are not in the wheel. `bundled_tools` listed `nodes` (deleted above) and
  `agent` (no such module for some time), so an inventory diff across releases
  showed capability that was not there. A new parity test asserts every name in
  `BUNDLED_TOOLS` resolves to a module under `agentos.tools.builtin`.

### Changed

- `UsageTracker.check_warning()` is removed. It had no callers; the
  `[budgets]` session ceilings replace it with a configurable, enforced
  equivalent.

- The Bankr user-skill allowlist — the half that carries skills published from
  a wallet on bankr.bot — is now empty. `stock-premium-lp-manager` was retired
  from it in favour of `aero-stock-lp`, which covers the same tokenized-equity
  LP workflow from the repository. Copies already installed keep working; the
  slug is no longer offered for browse or install.

### Fixed

- `agentos chat`, `agentos sessions`, `agentos skills`, and `agentos env` now
  send the resolved gateway token when they open their own WebSocket
  connection, and honour `AGENTOS_GATEWAY_URL` consistently. `resolve_auth`
  grants no loopback exemption in token mode, so setting `auth.mode = "token"`
  previously broke all four commands even on a purely local install — the
  token resolver existed (`default_gateway_token`) but these call sites never
  used it. `chat` additionally ignored `AGENTOS_GATEWAY_URL` entirely and
  always dialled the hardcoded `ws://localhost:18791/ws`.

- Bankr catalog cards all wear the Bankr brand mark again. `aero-stock-lp` is
  the one entry in `BankrBot/skills` whose `catalog.json` ships a `logo`, so it
  rendered that artwork while every other card in the partner tab showed the
  Bankr symbol. The Bankr source now ignores the payload's logo entirely —
  membership in the catalog is the brand, and a repository-side edit can no
  longer repaint a partner card's identity.

- The Control UI bootstrap endpoint no longer leaks host details to any website
  the operator visits. `{control_ui.base_path}/api/bootstrap` sat inside the
  prefix that is exempt from the loopback Origin guard, so with the default
  `cors.allowed_origins = ["*"]` any page could `fetch()` it cross-origin and
  read the absolute config file path (which reveals the OS username) along with
  the configured `auth_mode`. The bootstrap payload no longer carries
  `config_path` at all — the console reads it from the authenticated
  `doctor.status` RPC instead — and the Origin guard's Control UI exemption now
  stops at `{base_path}/api/`, so the shell and its fingerprinted assets stay
  exempt while every JSON route under the prefix is fenced on the loopback
  binds the guard covers. `AuthMiddleware` gets the same narrowing, with a
  single carve-out for `/api/bootstrap` itself, which the console must read
  before it holds a token. Fixes #351.

- `auth.mode = "password"` no longer admits the gateway unauthenticated. The
  mode was advertised and env-bound (`AGENTOS_AUTH_PASSWORD`) but had no branch
  in `AuthMiddleware.dispatch`, so it fell through to the unauthenticated pass
  and left the whole non-RPC surface — `/api/system/status`, `/api/config`,
  `/api/v1/files/upload`, `/api/audio/transcribe` — open on a loopback bind. Any
  typo'd mode did the same. `auth.mode` now validates against the modes the
  gateway actually implements (`none`, `token`, `trusted-proxy`) and refuses
  anything else at load time with a message naming the fix, and the middleware
  fails closed with `401` on any mode without an enforcement branch — the config
  object is read live, so a runtime mutation cannot reopen the hole. `auth.mode`
  is also case- and whitespace-normalized now (`" TOKEN "` loads as `token`), and
  `agentos.toml.example` plus the setup guide no longer list `password` as a
  choice. Closes #352.

- Credential masking no longer rewrites ordinary source code. The
  `Authorization` / `x-api-key` header names matched as substrings
  (`"requiresApiKey": False`), their value ran past the closing bracket
  (`{"xi-api-key": api_key}` lost its `}`), a bare number was masked as a
  credential, a vendor prefix matched mid-base64 (`AKIA…` inside an embedded
  font blob), and a PEM block spanning two adjacent string literals swallowed
  the code between them. Header names now match on a segment boundary, values
  stop at the punctuation that closes them and skip numbers and `<placeholder>`
  forms, prefixes need a left boundary, and a PEM span must have a base64 body.
  A PEM block in a `read_file` window is masked line by line, so the line
  numbers the reader computes its next `offset=` from stay correct.

- The `NAME=value` pass now recognises quoted keys (`"client_secret": "…"`), so
  credentials in JSON and YAML config are masked as the docstring always said
  they were.

### Security

- Installing a skill from ClawHub no longer unpacks the downloaded zip
  unbounded. `ClawHubSource.fetch` read every entry into memory with no cap on
  entry count or uncompressed size, so a few tens of KB of nested deflate — a
  classic zip bomb — could exhaust the gateway's memory and take the process
  down. The download is now streamed against a size ceiling — httpx gunzips a
  `Content-Encoding` body with no limit of its own, so a buffered read could
  have been filled before any zip cap got a say — and the archive is refused
  past an entry count, a per-entry size, and a total uncompressed size. Each
  entry is decompressed in chunks against a running total, so an archive that
  understates `ZipInfo.file_size` is caught mid-read rather than trusted. A
  hostile archive also fails closed rather than raising through the installer:
  an entry flagged encrypted, an unsupported compression method, or a truncated
  deflate stream reaches the caller as "no bundle", not as an exception that
  aborts a whole lockfile sync. Entry paths are also checked against
  Windows-style escapes (`..\`, `C:\`), which
  `posixpath.normpath` leaves intact; previously only the installer's resolve
  check caught those. Closes #357.

- `read_file`, `read_spreadsheet` and `grep_search` now mask credentials in the
  content they hand back to the model, and so do the two channels that quote
  file content alongside them: `edit_file`'s closest-match hint, and terminal
  output from a command that reads a credential file (`cat ~/.aws/credentials`).
  Previously the sensitive-path denylist was the only thing protecting a secrets
  file, and that denylist is lifted entirely under elevated-full mode — which
  cron `agent_turn` jobs run by default — so `read_file ~/.aws/credentials`
  returned the raw keys into the persisted transcript. Masking uses a
  non-reusable `«redacted:…»` sentinel, DSN and URL passwords included, so a
  value read out of a config file cannot be written back over the working one.

  How much of the pass runs depends on the file. Shape-matched credentials
  (`sk-…`, JWTs, PEM blocks) are masked everywhere; the name-driven pass, the
  only one that catches a shapeless secret like `aws_secret_access_key`, runs
  everywhere except source code, where it would mask identifiers and hand back
  code that no longer matches the file. Closes #355.

## [2026.8.24] - 2026-08-24

### Added

- Channel tool approvals are now native interactive surfaces. Telegram inline
  keyboards, Slack Block Kit actions, and Discord message components render an
  Approve/Deny pair for a gated tool call instead of asking the operator to type
  a reply. Every click is authorized before it is honoured: the clicker must
  pass the channel's own access policy and be an admitted paired user, the
  approval is bound to the `sessionKey` that raised it so a click from another
  session is refused, and the surface is offered only in DMs, where the session
  key is `PER_CHANNEL_PEER` and the approver is unambiguous. Slack request
  signatures are verified against the raw request body rather than a parsed
  form, so verification no longer depends on the ASGI body having survived a
  read. Closes #364.

- Cost visibility. A usage ledger records the cost of each turn and attributes
  it per tool and per skill through a `ContextVar` that follows the call into
  nested execution, and a new `agentos cost` command queries it with filters for
  session, model, tool, skill, and time range, plus export. The router gains a
  `cost_aware` flag (on by default) that substitutes the cheapest tier capable
  of the request; image-only tiers are filtered out before the comparison, so a
  text request is never routed to an image model. Closes #366.

- Aeon (`aeonfun/aeon`) joins Robinhood, Bankr, and Capminal as a Partner Skills
  source in the Skills hub, with the partner tabs ordered Robinhood, Bankr,
  Aeon, Capminal, Community.

### Fixed

- The gateway no longer accepts an auth token from the query string, where it
  would be captured by proxy and server access logs; the uvicorn access log is
  gated behind `config.debug` for the same reason. Closes #350.

- Rate limiting reads `X-Forwarded-For` only from a verified trusted proxy, and
  the per-client dict is bounded, so a spoofed header can neither bypass the
  limiter nor grow it without limit. Closes #354.

- Unhandled gateway exceptions are redacted before they reach the client; the
  detail is shown only when `debug` is set. Closes #353.

- The `browser` tool refuses `data:` URLs, which could otherwise carry a page
  past the SSRF check and the domain allowlist; `about:blank` remains the only
  permitted hostless target. Closes #356.

- The `usage cost` fallback path declines query filters it cannot honour instead
  of silently dropping them and returning an empty result set.

### Removed

- Dead configuration keys that no code read: `sandbox.network_default` (#360),
  the memory daily-note keys (#405), and `subagents.archive_after_minutes`
  (#407).

### Docs

- `cron_default_mode` — the default elevation posture for unattended cron jobs,
  shipped in 2026.8.21 — is now documented where it is set and where it is read:
  `agentos.toml.example`, the bundled `agentos` skill, and the approvals and
  permissions guide. (#413)

## [2026.8.23] - 2026-08-23

### Added

- A `browser` built-in drives a real browser from the agent, backed by the
  agent-browser CLI (Vercel Labs, Apache-2.0): navigate, read a page as an
  accessibility snapshot with element refs, click, type, fill, wait, run
  JavaScript, answer native dialogs, and screenshot. It runs managed and
  headless by default; attach mode drives the operator's own browser when they
  opt in. Policy is enforced in AgentOS rather than delegated to the engine —
  SSRF checks on navigate and on the post-redirect URL plus a private-page guard
  on reads, `file:` refused while `data:`/`about:` pass, `eval` SSRF-pre-scanned
  in both modes with an opt-in `restrict_evaluate` denylist and a post-eval URL
  recheck, `type`/`fill` refusing credential-shaped text, and every payload the
  engine returns crossing into the transcript inside the untrusted envelope and
  through credential redaction. The engine subprocess starts from a minimal
  environment, never `os.environ`, so the gateway token and provider keys are
  unreachable from it. An optional `allowed_domains` bounds navigation, and the
  tool sits in `group:web`, so denying web denies it.

- Provider failover is now health-aware. A circuit breaker counts consecutive
  provider-health failures (overload / gateway 5xx, transport errors, rate
  limits) per configured provider id; after
  `llm.circuit_breaker.failure_threshold` failures (default 3) the provider is
  skipped for a cooldown window (default 60s, doubling per consecutive trip up to
  `max_cooldown_seconds`), and one half-open probe per window re-closes it when
  the provider recovers. Failover used to be purely reactive and per-request —
  every turn during an outage paid the full timeout on the dead primary before
  falling back, because `ModelSelector` reset to the primary each turn. Breaker
  state is shared across per-turn selector clones, so detection is paid once
  per outage instead of once per turn. Request-shaped failures (unknown model,
  bad request, context overflow, auth, billing) never trip the breaker, and if
  every link in the chain is in cooldown the primary is still used. State is
  surfaced in `agentos providers status` (new `circuit` column),
  `agentos doctor` (`provider.circuit.open` / `provider.circuit.half_open`), and
  `GET /api/system/status` (`circuitBreaker` / `circuitBreakers`). (#365)

### Changed

- Chart artifacts in the Web UI download as a rendered screenshot image instead
  of a raw JSON link, so the button hands over the chart people actually see.

### Fixed

- A pinned turn no longer shows another turn's router-fx strip. The
  `route_pinned` early-return swept only live strips from the dock, so a settled
  strip from an earlier turn lingered above the composer and read as this turn's
  selection even though the composer pill showed the pinned model. Every
  router-fx strip for the current session is now swept on the pinned path —
  live and settled alike — while strips from other sessions are left untouched.
  (#345)

- Skill dependency installs work for every kind a skill can declare. Three
  code paths carried their own idea of what `install.kind` meant — the Skills
  page executor knew `brew`/`uv`/`download`, the `install_skill_deps` tool knew
  `brew`/`node`/`go`/`uv`, and the install hints rendered a third, different set — so the
  seven bundled gmgn skills, which declare `kind: npm`, were uninstallable
  through both executors ("Unsupported install kind: npm"), and `apt` failed
  the same way. All three now read one canonical vocabulary and one command
  builder in `agentos/skills/install_kinds.py`: `brew`, `npm`, `go`, `uv`,
  `download`, and `apt`, with `node` kept working as an alias for `npm`. The
  command shown as an install hint is now literally the command that runs.
  `apt` (needs root) and `download` (needs a fetch plus a chmod) stay
  hint-only, and say so instead of reading as unsupported. A `uv` spec that
  declares `bins` installs with `uv tool install`; one that doesn't — a library
  like `openpyxl` — keeps using `uv pip install`, which the agent tool used to
  get wrong. Pinned versions (`gmgn-cli@1.2.3`, `openpyxl>=3.1`) now survive the
  value allowlists instead of losing their install hint, an `apt` package can no
  longer end in the `-` that turns an install line into a removal, and the
  `download` hint validates and quotes its URL rather than interpolating it
  raw. (#358)

## [2026.8.21] - 2026-08-21

### Added

- Inbound Telegram voice messages, audio files, and round video notes are
  transcribed before the turn is built, and the speech-to-text output becomes
  the message text. A voice note used to reach the agent as the placeholder
  `[voice]` with the audio stripped, so the only way to be understood on a
  phone was to type. The ElevenLabs STT call was factored out of
  `audio_transcription.py` into a shared helper and wired into channel message
  ingestion, so `voice`, `audio`, and `video_note` payloads all take the same
  path. A default 120-second duration limit (configurable through
  `max_voice_duration_s`) and a 30 MB size limit are checked before the
  download; over either limit, or on an STT failure, the sender gets a reply
  saying so and the message still reaches the agent under its placeholder
  rather than being dropped. The channel download limit is relaxed to 30 MB for
  `audio/` and `video/` types while the attachment whitelist stays strict —
  raw audio is stripped once transcribed. Group mention detection now also
  admits replies that target the bot, by user id or by username. (#312, #317)
- `agentos skills init <name>` scaffolds a local custom skill that passes the
  publish gate on the first try: a `SKILL.md` with clean YAML frontmatter and a
  body long enough to clear the 20-character validation, plus
  `scripts/run.py` and its entrypoint mapping under `--with-script`. Names are
  validated against `^[a-zA-Z0-9][a-zA-Z0-9.-]{0,63}$` so a name cannot walk out
  of the target directory, and an existing file is only overwritten with
  `--force`; other files in the directory are left alone. The target resolves
  through the usual layer order — `~/.agentos/skills`, `~/.agents/skills`,
  `<workspace>/.agents/skills`, `<workspace>/skills`. (#316, #321)

### Changed

- Scheduled agent turns (`agent_run` cron jobs) are elevated by default,
  running in `bypass` mode instead of needing a per-job opt-in — an unattended
  turn that stops to ask for an approval nobody is there to give is a turn that
  does nothing. The new global `cron_default_mode` field on `PermissionsConfig`
  holds the default, and the router resolves effective elevation at execution
  time from the `handler_key` now carried in the cron envelope. Every other
  unattended kind — reminders, system events, script runs — stays strictly
  unelevated, and an explicit `--no-elevated` on any of them is honoured rather
  than rejected. Elevated warnings log `source="config"` or `source="job"` so
  the log says how elevation was granted, and the effective value is shown on
  Web UI job cards and in the CLI `cron list` table. The wire-level `elevated`
  field keeps meaning "explicit override", so existing jobs read back
  unchanged. (#311, #323)
- Web content the agent reads is wrapped in the same `<untrusted source='…'>`
  envelope the system prompt teaches, through a new `wrap_untrusted_boundary`
  helper in `safety/injection_guard.py`. `web_fetch` had its own
  `<external-content>` tag, which the dispatch layer did not recognize: a
  tool-call marker planted in a fetched page got zero enforcement. It now
  trips the refusal path like any other untrusted fragment. Only nested
  `<untrusted>` markers are entity-escaped, so the page itself passes through
  verbatim and stays readable; the escaping is idempotent, so truncation
  re-wrapping still works. `http_request` wraps its text `body` and
  `body_preview` with the fetched URL as the source, with the 10k text cap
  applying to the payload rather than the envelope. Binary and base64 paths are
  unchanged, and `web_search`/`x_search` snippets stay out of scope. (#339,
  #340)
- The core system prompt drops `## AgentOS CLI Quick Reference` — two
  hardcoded commands that drift from the real CLI, whose canonical references
  are the bundled `agentos` skill and `docs/cli.md` — and folds `## Workspace`
  into `## Runtime`, keeping each line's gating so OS and shell stay full-mode
  only and the working-directory line keeps its own condition. Reply Guidelines
  now open with "Lead with the answer or outcome; keep supporting detail after
  it". Net −124 characters, about 31 tokens, on a full-mode render. (#343,
  #344)

### Fixed

- Section headings in the rendered system prompt are no longer glued to the
  section above them. Any section whose last line was conditional closed with
  `{% endif -%}`, and the right-trim dash swallowed the blank line before the
  next heading — every full-mode prompt shipped so far rendered `# Agent` stuck
  onto `## Product Identity`, `## Image Generation` onto `## Memory Recall`,
  and `## Memory Recall` onto `## Memory Write Guidance`. (#343, #344)

## [2026.8.19] - 2026-08-19

### Added

- A cron job's `script` path may contain `{job_id}`, which the scheduler
  replaces with the created job's own id before the job is persisted, and the
  `cron` tool's add result now reports the resolved path as `script_path`. A
  job that keeps its files in a directory named after itself could not name that
  directory at creation time, because the id is minted by the create: the only
  route there was to stage the script elsewhere, add the job against the staging
  path, move the file, and repoint the job — four steps during which a live job
  points at a path it will not keep, and any run abandoned midway leaves files
  behind that nothing can attribute to a job. One `add` now does it. Works for
  a `script` job and for an `agent_turn` job's pre-run script, from the tool,
  the CLI, and the RPC surface alike, because the substitution happens in
  `SchedulerOps`. A stored path that somehow still holds the placeholder refuses
  to run rather than creating a directory called `{job_id}`. (#332)
- Skills can pin a section so `skill_view` returns it wherever it sits in the
  file: `<!-- always -->` on the line directly above a heading. Over the read
  ceiling `skill_view` returns a skill's opening sections plus an index of the
  rest, so position in the file decided what a model actually read — and a rule
  written into a large skill's tail was never seen unless the model thought to
  ask for that section. `senior-unilp-manager` is 44k characters against a 10k
  ceiling, and two merged fixes wrote their rules past the cut; neither reached
  the model, and the next run repeated both mistakes. Pinned sections come out
  of the same ceiling rather than adding to it — at most half of it, with the
  opening taking what is left — and the index marks them as already shown.
  `skill_view.outlined` is now logged at INFO with a `pinned` count, because it
  is the only event that says most of a skill did not reach the model. Pinning
  governs what one `skill_view` call returns; what survives into later turns is
  the transcript's business, fixed separately in #334. (#332)

### Fixed

- A tool may now declare the ceiling its own results are persisted under, and
  `skill_view` sets one from `[skills].max_skill_view_chars`. Every tool result
  was written to the transcript truncated to its first 2,000 characters, so a
  skill body read on one turn came back on the next as an opening that stops
  mid-sentence — and 40 of the 50 bundled skills are larger than that. A session
  read `senior-unilp-manager`, saw the pinned directory rule from #332 in an
  11,996-character result, and one turn later replayed 2,000 characters that did
  not contain it and wrote the files flat. Nothing downstream could recover it:
  the request builder compacts from what was persisted, not from the original,
  so the layer under no pressure at all — writing one SQLite row — was cutting
  harder, and more crudely, than the layer that has a budget to defend. The
  ceiling is resolved when a result is persisted rather than at registration, so
  it follows a config change, and a tool that declares nothing keeps the 2,000
  characters that suit volatile output. (#334)

- Web chat: copying an assistant message no longer prepends the collapsible
  reasoning block. `extractBubbleText()` cloned `.msg-body` and stripped only
  `.msg-actions` and `.msg-meta`, so the `Thinking` summary label — and the
  full reasoning body once the block had been expanded — landed in the
  clipboard ahead of the reply. `.thinking-block` now joins the strip list, so
  copy yields the reply text alone whether the block is collapsed or expanded.
  (#322)

### Changed

- The core system prompt was rewritten and is now gated by surface. Tool Call
  Style teaches parallel tool batches and a verify-with-tools bias instead of
  the no-op "wait for tool results" line, a new Task Execution block carries a
  persistence rule with an anti-stuck escape hatch and an explicit
  approval-denial boundary, and Safety gained irreversible/outward action
  confirmation, secrets handling, and the `<untrusted>` envelope convention
  with its coverage caveat. Reply Tags, Messaging and Reactions now render only
  when at least one channel adapter is configured, and Silent Replies only for
  sessions that can receive internal system events, so a pure Web UI/CLI
  gateway no longer teaches reply-tag syntax, emoji reactions, or a `NO_REPLY`
  sentinel it can never legitimately use — roughly 257 tokens saved per
  full-mode session. Both gates are boot-time or session-kind stable, so the
  cacheable base prompt does not churn. Section-level contract tests pin each
  block per prompt mode and tool set. (#336, #338)
- `senior-unilp-manager`'s monitor layout is now one `cron` call and one pinned
  section. "Files on disk for a monitor" became "Setting up a monitor", shrank
  from 4 238 characters to roughly 2 100 — the stage-add-move-repoint sequence
  it existed to explain is replaced by `script="senior-unilp-manager/{job_id}/
  tick.sh"` — and carries `<!-- always -->`, so it reaches the model whatever
  the read ceiling is. The rule that a monitor is a `script` job unless a model
  in the loop was asked for moved into that section for the same reason: it sat
  past the cut too, and the run that motivated this shipped an agent task the
  user had to correct by hand. (#332)
- `senior-unilp-manager` now defaults the ratchet monitor to a `script` cron
  job. "Wiring it to cron" leads with the `job_kind="script"` shape and states
  the rule outright: if the user did not say which shape they want, schedule
  the script job. `agent_turn` is documented as the explicit opt-in for when a
  model is wanted in the loop — to summarize or escalate in its own words —
  with its cost named, a full turn on every tick of a job that is almost always
  a no-op. `tick` already reconciles and fires in one process, so the script
  shape costs no model call, takes no `tool_policy`, and with `--alert-only`
  stays quiet on a healthy ratchet. Instructions only; no behaviour changed.
  (#325)
- `senior-unilp-manager` now has a file layout for the monitors it sets up.
  Every file a ratchet monitor needs — `tick.sh`, any helper the agent writes
  for it, any scratch the run keeps — lives under
  `~/.agentos/scripts/senior-unilp-manager/<cron_id>/` and nowhere else, so the
  mapping from job to files is one-to-one: delete the job, delete the
  directory. Previously the skill said only "a script under
  `~/.agentos/scripts/`", and each run invented its own names in a directory
  shared with every other skill's jobs, which left nothing that could be
  cleaned up when a mandate was disarmed. The skill now also says explicitly
  that mandate state is not part of that directory — the mandate JSON, the
  write-ahead log, and the lock stay under `$UNILP_STATE_DIR` /
  `$AGENTOS_HOME/state/unilp` / `~/.agentos/state/unilp` — and spells out the
  stage-then-repoint ordering, since a cron id does not exist until its job
  does, and the teardown that the layout is there to make possible: remove the
  job, then remove its directory. Skill instructions only; subdirectories under
  the scripts directory were already supported. (#326)

## [2026.8.17] - 2026-08-17

### Added

- The in-agent `cron` tool can name where a job announces. `add` takes an
  optional `delivery` object — `mode` (`origin`, `channel`, or `none`),
  `channel_name`, `channel_id`, `account_id`, `thread_id`, and `best_effort` —
  so "every weekday at 9, post the digest to the ops group" no longer has to be
  created in the chat that will receive it. Omitting `delivery` keeps the
  existing behaviour exactly: the job reports back to the calling conversation.
  The destination is validated when the job is saved rather than when it fires,
  so an unconfigured channel name, an AgentOS session key passed where the
  provider's chat id belongs, or a destination paired with a mode that cannot
  route to it are all refused with an error naming the problem — silently
  falling back to the calling chat is what made a misdirected job look like a
  working one. The `add` response echoes the resolved destination, and a clone
  given a `delivery` is redirected rather than inheriting the source's.
  Choosing a channel requires an interactive CLI or Web caller and a
  `session_target` other than `main`; webhook delivery and failure destinations
  remain CLI-, Web-, and RPC-only. (#310)
- `cron(action="update")` accepts `delivery` too, so moving an existing job's
  announcement is an edit rather than a rebuild. Refusing it left the model one
  route to "post that job to Telegram instead" — remove the job and add a
  replacement — which threw away the job id the user had just named along with
  its whole run history, and in practice the refusal message pointed at the CLI,
  the Web UI, and the RPC, none of which an agent in a chat can reach, so the
  same failing call was retried until the turn was interrupted. A repoint keeps
  the job's id, run history, `ws_topic` (so existing websocket subscribers stay
  attached), and failure destination, and applies the same gates as `add`:
  `mode='channel'` needs an interactive CLI or Web caller and a `session_target`
  other than `main`, the recipient is validated at save time, and a chat caller
  cannot repoint a job that already reports somewhere that chat cannot address.
  (#310)
- Sessions can be renamed. A new `sessions.rename` RPC sets (or clears) a
  session's `display_name`, and it is reachable from every surface:
  `agentos sessions rename <id> "<name>"` (`--clear` drops it), `/rename <name>`
  in CLI chat — gateway and standalone — and in chat channels, and
  click-to-edit on a row in the Web UI session list. `agentos sessions list`
  grows a `Name` column and a `--search`/`-q` filter that matches the name,
  key, subject, or model; `sessions.list` now ships `derived_title`, so the Web
  UI's existing name-aware filter works on real data. Names are normalized in
  one place (`agentos.session.naming`): whitespace collapses to a single line,
  control characters are dropped, the value is capped at 120 characters, and an
  empty name clears the label so the derived title takes over. Because renames
  resolve a target the same way `/resume` does, a session can be renamed by its
  current name instead of its full key — an exact name now beats a prefix
  match, so naming a session `agent` no longer collides with every session
  key. `--search` widens its fetch beyond `--limit` so it can reach older
  sessions, and names are Rich-escaped everywhere the CLI prints them, so a
  name containing `[/]` can no longer break `sessions list`. No migration is
  required — the `display_name` column already existed. (#248)
- Renaming reaches the Chat view itself. The header `⋯` menu gains **Rename
  session**, which edits the name in place — Enter saves, Escape cancels the
  edit without closing the menu, and an empty value clears the name. Once a
  session has one, the header chip shows the name instead of the key (the key
  stays in the chip's tooltip and in **Copy session key**), and the session
  switcher lists each renamed session by name with its key underneath. The
  switcher's search now matches the name and the derived title as well as the
  key, so a session is findable there by the label it was given — the same
  search behaviour the Sessions page already had. Agents can rename too: the
  new `session_rename` tool sets or clears the name of the session it is
  running in — and only that one — so "call this one X" works as a prompt. It
  shares the `agentos.session.naming` normalizer with every other rename path,
  and reports rather than silently succeeding when storage cannot persist the
  change. (#248)

### Fixed

- The in-agent `cron` tool can now edit a job instead of replacing it. Asking
  the agent in chat to change a scheduled job's prompt — or to "clone this one
  but …" — used to leave it no strategy but `add` a new job and `remove` the
  original, which deleted the job the user wanted to keep and reset every
  setting the re-create did not name: an `agent_turn` fell back to `reminder`,
  a job pinned to `Asia/Bangkok` moved to UTC, its tool policy was dropped,
  and its output started landing in the current chat instead of the channel it
  reported to. The tool gains `action="update"` (patch in place, keeping the
  job id), `action="get"` (the full record — kind, tz, schedule, session
  target, delivery, tool policy, wake mode, timeout, script fields), a
  `clone_from` parameter on `add` that inherits every setting of the source and
  overrides only what is passed, and a `name` parameter so a job's display name
  no longer has to be its prompt. Jobs carrying a script or
  `tool_policy.elevated` stay operator-only to clone or update, a channel
  caller cannot clone or rewrite a job that reports to a destination its own
  chat cannot address, and `action="get"` names a webhook's host without
  disclosing the URL path or token. (#309)
- Rescheduling a one-shot cron job onto a recurring expression no longer leaves
  `delete_after_run` set, which made the edited job delete itself after its
  first fire. Converting a job away from `agent_turn` now drops a stranded
  `tool_policy.elevated` instead of persisting a combination `cron add` refuses
  to create, and a `tool_policy` sent alongside a kind change is validated
  against the new kind rather than the outgoing one. All three are in
  `SchedulerOps.update`, so the `cron.update` RPC and the Web UI edit flow get
  them too.

## [2026.8.15] - 2026-08-15

### Added

- A built-in Tavily provider joins the `web_search` backends. It is a runtime
  provider like `brave` and `duckduckgo` — not a skill-only engine — so
  selecting `tavily` and setting `TAVILY_API_KEY` is all it takes; onboarding
  offers it alongside the other keyed providers, and the key is redacted in
  logs and transcripts like every other credential.
- The Web UI now shows a "new release available" banner, closing the gap with
  the CLI, which has warned about outdated installs for several releases. An
  `updates.check` RPC method reports the running version, the latest version on
  PyPI and a `up-to-date` / `outdated` / `offline` status; the console renders
  the banner only on `outdated`. The check reuses the CLI's cached PyPI state
  with its own `webui` slot, so the browser does not add PyPI traffic beyond
  the existing interval, and it stays silent when `AGENTOS_NO_UPDATE_NOTICE=1`
  is set or `updates.notify` is off. `pypi_client` and `version_utils` moved
  from `agentos.cli` to `agentos.compat` so the gateway can use them without
  importing the CLI.
- Gmail/GitHub-style navigation chords land in the Web UI: press `g`, then a
  destination key within 1.5s, and every sidebar view is reachable from the
  keyboard. The prefix re-arms on a repeated `g` and is cancelled by Escape or
  any held modifier; each chord closes the mobile drawer and moves focus to the
  main content region. The `?` cheat sheet renders multi-step chords through
  the `t()` seam, and `docs/web-ui.md` documents the full set.

### Fixed

- The per-message hover toolbar (copy / regenerate / edit) could not be
  clicked. It sits in the outer gutter, outside the `.msg` box that carries the
  `:hover` state, so crossing the 8px margin dropped the hover and faded the
  buttons out — while the reveal animation slid them away from the incoming
  pointer. A transparent bridge pseudo-element now makes the hit region
  continuous and the `translateX` reveal is gone. The bridge is suppressed on
  narrow viewports and under `hover: none`, where the toolbar is already in
  normal flow.
- `x_search` could hand a single attempt a timeout slightly larger than the
  whole budget it was meant to fit inside. The per-attempt timeout came from
  `deadline - time.monotonic()`, and on a coarse clock (Windows resolves
  `monotonic()` to ~15.6ms) both reads land in the same tick, so the expression
  collapses to a rounded `(t + total) - t` that can exceed `total`. The
  per-attempt timeout is now capped on the total budget as well, so the
  invariant holds at any clock granularity.

## [2026.8.13] - 2026-08-13

### Added

- A bundled `poolsdotfun-token-launcher` crypto skill launches a token on
  pools.fun through the `PartyFactory` on Robinhood Chain (4663) and manages the
  creator fees on the `PartyLocker` afterwards. A launch is one irreversible
  transaction: it CREATE2-deploys a fixed-supply ERC20 with no owner and no mint
  function, opens a SushiSwap V3 pool at the 1% fee tier, and mints the whole
  supply as a single-sided full-range position whose LP NFT goes to the locker
  permanently — the launcher never holds it. The chain and RPC endpoint are
  built in, so there is nothing to configure beyond `POOLSFUN_PRIVATE_KEY`.
- The skill separates reading from signing. `pools_read.py` quotes cost, opening
  price and pool state, simulates a launch and mines a launch salt using only a
  `--from` address; `pools_write.py` is the only script that can sign. A launch
  plan is hashed, so the transaction that broadcasts is provably the one that
  was quoted.
- `PINATA_JWT` is optional and needed only to attach a token image. It is
  deliberately not declared as a skill requirement, so a launch without a logo
  still works on a machine where Pinata was never configured.

### Fixed

- The launcher can now find a logo the user attached in chat. Chat attachments
  arrive in two shapes — staged to disk under a sha256 name, or inlined as
  base64 in the transcript — and the skill previously looked only at the media
  directory, so an inlined image was missed and a stale disk blob could be
  uploaded in its place. A `find-image` read command now resolves the image from
  the transcript first, materializes it, and warns when the only candidate is
  older than the request.

## [2026.8.12] - 2026-08-12

### Added

- The Web UI now shows the model thinking. Reasoning arrives as a typed
  `ThinkingDeltaEvent` from the Anthropic, OpenAI-compatible and Ollama
  providers — including models that emit `<think>` tags inline, split back out
  of the text stream as it arrives — and renders as a live collapsible block
  that folds itself the moment the reply text starts. A fresh block opens per
  reasoning round, so mid-turn work stays visible rather than being appended to
  the first one. History carries a `has_thinking` flag so a reloaded thread
  still offers the block. `control_ui.show_thinking` (default true) gates the
  whole surface.
- Thinking is web-only by construction. It travels on a `session.event.thinking`
  emit path and a CONTROL_ONLY `chat.thinking` RPC, so channel adapters never
  receive it — reasoning is not something to page a Telegram or Discord thread
  with.
- The chat composer now carries a route picker, so choosing which model answers
  no longer means remembering a slash command. It lists the text tiers your
  `[agentos_router]` config actually defines — labelled with the model each
  resolves to, e.g. `c1 · gpt-5.6-luna` — plus `Auto`, which hands routing back
  to the Pilot Router and reports the tier it last chose (`Auto · c2`) so
  automatic routing stays legible. The pin is read back from the gateway over a
  new `router.hold.get` RPC rather than mirrored in the browser, so a reload
  shows the pin that is really in force and a pin set from `/c3` and one set
  from the picker agree. With no Pilot Router configured the control is disabled
  rather than hidden, keeping the composer from reflowing when the router is
  toggled.
- The picker is a searchable list, so the choice is not limited to the four
  configured tiers: it also offers every model of the active provider, and
  `/use <model-id>` does the same from a slash command on web, TUI and channels.
  A directly-named model rides on the default tier, inheriting the thinking
  level and pricing baseline that live on a tier and not on a model id — which
  also keeps the router step's `tiers[hold.tier]` lookup valid. Only the active
  provider's models are offered: every turn runs through the single configured
  `llm.provider` (a tier's `provider` field is metadata, not a client selector),
  so anything else is refused when chosen rather than failing on the next turn.
  `/use` is a new verb rather than an argument to `/model`, whose argument
  already filters the listing. Only models the provider publishes in its
  catalog can be pinned — on OpenCAP that is the bare canonical ids, not the
  namespaced `<upstream>/<model>` aliases its inference endpoint also answers to.
- The router-fx strip is suppressed while a tier or model is pinned. It exists
  to show the router weighing candidates and settling on one; a pin decides the
  route up front, so the animation was dramatizing a deliberation that never
  happened and restating the composer's own picker every turn. It returns the
  moment routing goes back to Auto.
- A pin now withdraws the model's own `router_control` tool for the duration,
  along with its target menu in the system prompt. The user's choice already
  outranked the model inside the router step; leaving the lever on the surface
  only invited calls that could not take effect and paid tokens to describe
  them. Holds the model installs for itself are unaffected — hiding the tool on
  its own hold would strand a session on a transient escalation.

### Changed

- **Breaking:** a tier pin set by a user is now sticky. `/c0`…`/c3` — in the Web
  UI, the TUI, and every channel — hold until `/auto` clears them instead of
  lapsing after ten idle minutes. A pin is a standing instruction, and a
  selection that silently reverted would have made the new composer control lie
  about what is running. The practical consequence is a bill: pin `/c3` and
  forget, and every later turn keeps paying for `c3` until someone runs `/auto`.
  Routing the model chooses for itself mid-turn is unchanged and still lapses on
  its own. Two things still outrank a pin, both pre-existing: image turns are
  routed to a vision tier before pins are consulted (the Web UI flags such a
  turn), and a pinned turn skips the large-context tier floor, so it fails at
  the provider rather than being upgraded if the conversation outgrows the
  pinned model's context window.

### Fixed

- A hub skill whose `SKILL.md` renames itself no longer renders as a local one.
  The lockfile is keyed by the install directory but was read back by the name
  the frontmatter declares, and published skills do rename themselves — hub
  `ytdlp-transcript` ships a manifest named `youtube-transcript`. The lookup
  missed, so an ordinary hub install appeared under "Your local skills" with no
  source, no version, no scan facts and neither a Remove nor an Update button,
  the same wrong row reaching `agentos skills list` and the agent's
  `skill_list`. Entries now join by the resolved path they already record,
  falling back to the name for entries written before `path` existed. The
  removability guard read the manifest name too and so reported a removable
  install as an orphan; `skills.uninstall` and `skills.update` — and the CLI's
  no-gateway uninstall path — now translate to the install key the same way.
  The wire contract is unchanged and existing installs heal themselves: no
  lockfile migration, no re-install.
- OpenCAP and Bankr routes reported `supports_reasoning=False` for every model,
  which silently no-oped a tier's `thinking_level`.

## [2026.8.11] - 2026-08-11

### Added

- A built-in `x_search` tool searches X (Twitter) through xAI's server-side
  search on the Responses API, returning a synthesized answer with citations
  rather than the ranked pages a web search provider returns — so it is its own
  tool, not a `web_search` backend. It joins `group:web`, so denying that group
  also cuts the route to `api.x.ai`, and it is allowed for cron agents next to
  `web_fetch`/`web_search` because it is read-only. Visibility follows the
  `image_generation` pattern: an install with no xAI credential never pays the
  tool's schema on a provider call. Retries are deadline-aware —
  `timeout_seconds` bounds one attempt and `total_timeout_seconds` the whole
  call — and `base_url` must be HTTPS and is refused if it resolves to a
  metadata endpoint. `x_search` bills xAI directly and does not appear in
  `agentos cost`. Configure it at `[x_search]` with hot-apply, from the Setup
  page, or with `agentos configure x-search`. (Fixes #277)
- SuperGrok and X Premium+ subscribers can now sign in to xAI instead of pasting
  an API key, which is the only way to spend a subscription on `x_search` — xAI
  sells API credit and subscriptions separately, and a subscriber holds no key.
  `agentos auth login xai` runs the device-code flow, tokens land in
  `~/.agentos/auth.json` (0600) and refresh themselves, `agentos auth status`
  reports the login without printing a token, and `agentos auth logout xai`
  forgets it. OAuth is preferred over `XAI_API_KEY` at call time, and
  `credential_source` says which one ran. Discovery and inference origins are
  pinned to HTTPS on `x.ai`/`*.x.ai` on both the login and refresh paths, a
  `403` on refresh is reported as a tier gate rather than a re-login prompt, a
  terminal refusal quarantines the dead tokens, and refresh is serialized by a
  lock because xAI's refresh tokens are single-use. The Setup page can drive the
  same flow without blocking, over a split `start`/`poll` pair, and offers
  "Sign out of xAI" once signed in; the device code never crosses to the
  browser. Signing out forgets local tokens only — nothing is revoked at xAI,
  and `x_search` falls back to `XAI_API_KEY` if one is set.
- Every Web UI view now resolves its copy through the i18n seam. The shell and
  all sixteen views — chat, setup, config, settings, agents, sessions, usage,
  skills, channels, mcp, cron and the rest — read from per-namespace catalogs
  instead of carrying hardcoded English, with an `I18N_MIGRATED` ESLint ledger
  guarding each migrated file against regressions. Two rules back it:
  user-facing copy must come from `t()`, and `t()` must be called at render
  time, since a module-scope call freezes the locale at boot. Catalogs are
  registered per namespace so a view's copy stays out of the entry chunk,
  numeric placeholders format through `Intl.NumberFormat`, and malformed locale
  tags are rejected at registration rather than throwing later in `tPlural()`.
  The visible language is unchanged. (Fixes #138, #257, #258, #259, #260, #261)
- Translation requests now route to the cheapest tier. The router scores
  reasoning difficulty rather than task type, so an ordinary "translate this"
  landed on `c1` even in English — and because the Pilot corpus is English-only,
  the same request drifted a tier in either direction depending only on the
  language it was written in: measured against the English baseline, `trivial`
  moved from `c0` to `c1`/`c2` in 13 of 14 languages, while a genuinely hard
  request in Chinese, Japanese, or Thai *dropped* to `c1`. A deterministic
  detector now recognises a translate verb in the first or last paragraph of a
  turn across English, Vietnamese, Chinese, Japanese, Korean, Thai, Indonesian,
  French, Spanish, German, Portuguese, Russian, Arabic, and Hindi, and caps the
  turn at `agentos_router.translate_ceiling_tier` (default `c0`; set
  `translate_ceiling_enabled = false` to turn it off, or pick the tier in the
  setup wizard's **Translation cap** field). Every detected translation is
  capped, extras and all — a complaint upgrade, the large-context floor, and a
  programming language named as the target ("translate this Python module to
  Rust", a request to write code) are the only things that override it. Verb
  matching is word-bounded and guarded against overloaded stems, so Vietnamese
  `giao dịch`/`dịch vụ`, English "address translation bug", and Thai `แปลก` are
  not mistaken for translation work.

### Fixed

- Streaming channels show the typing indicator again while the model is still
  thinking. Since Telegram gained `send_streaming` its stream policy resolved to
  `adapter_stream`, which suppressed the indicator for the whole turn — and
  nothing can be streamed before the first token, so a user waiting out model
  latency and tool calls saw nothing at all. Telegram and Discord now type until
  the first chunk reaches the chat and drop the indicator the moment it lands,
  rather than either suppressing it for the run or letting it flicker back under
  a message that is already being edited. `typing_final` and `final_only`
  adapters are unchanged. (Fixes #255)
- External links in the Web UI transcript open in a new tab instead of replacing
  the chat, and a rejected sign-out is reported as a sign-out failure rather
  than "Sign-in failed" — both paths used to render through one label, pointing
  the operator at the wrong thing right after a successful sign-in.
- Writing the auth token store no longer fails on platforms without POSIX mode
  bits.

## [2026.8.9] - 2026-08-09

### Added

- Telegram replies now stream: AgentOS posts one message and edits it as the
  answer arrives, instead of showing a typing indicator for the whole run and
  then dropping the finished answer in at once. Edits are throttled to
  Telegram's stricter rate limit, answers longer than 4096 characters roll over
  into a follow-up message, and a burst of `429`s degrades to a single final
  send with the full text intact. Adapters that implement streaming (Slack,
  Discord, Telegram, Microsoft Teams) now declare the `streaming` capability, so
  the manifest and the Channels page reflect what they actually do.
  (Fixes #141)

### Changed

- The seven bundled GMGN skills now declare `category: crypto`, so the Skills
  page files them under **AgentOS Crypto Skills** instead of "AgentOS Normal
  Skills", and each card and detail dialog wears the GMGN mark badged with that
  skill's own emoji rather than the generic package glyph. The mark is chosen on
  `provenance.origin` behind the same shipped/bundled gate as the group itself,
  so a local drop-in cannot mint it, and it ships with the client, so no card
  fetches a remote image. Skill names are unchanged. (Fixes #246)
- A model's price, context window, max output and image support are now declared
  once, in `agentos.model_registry`; the pricing table, the catalog's window
  fallbacks and the router tier defaults are derived from it instead of
  restating it. Bumping a tier default used to mean editing four or five files
  by hand with nothing checking that you did — and because both lookup tables
  fail open in opposite directions, a forgotten entry produced a plausible wrong
  number rather than an error. Shipping a tier default whose model is not
  declared now fails at import. No prices or windows change. (Fixes #140)

### Fixed

- Shell workspace lockdown no longer misses a redirection whose operator has no
  whitespace around it. `echo x>/etc/passwd` and `cat<in>/etc/x` used to parse as
  having no write target at all, because the scan required a space or
  start-of-string before `>`; the same anchor bug was in the `tee` parser. File
  descriptor duplications (`2>&1`, `>&2`, `2>&-`) are blanked before the scan, so
  dropping the anchor does not turn every `2>&1` into a write to a file named
  `1`. (Fixes #197)

## [2026.8.7] - 2026-08-07

### Fixed

- Switching from a cloud LLM provider back to a local one no longer disables the
  router or leaves it pinned to the cloud provider's tier profile. (Fixes #189)
- Gateway boot and `agentos doctor` now warn when the bundled React Control UI is
  older than the frontend sources in a checkout (`gateway.control_ui.dist_stale`),
  instead of reporting a clean bill of health while serving a stale web UI. The
  warning is advisory and never gates readiness — source mtimes are a hint, not
  an oracle. Wheel installs ship no frontend sources and are never flagged.
  (Fixes #200)

### Added

- Onboarding remembers a per-provider profile when you switch LLM providers and
  restores it when you return: the model, the non-secret connection settings
  (`base_url`, `proxy`, `api_key_env`, `max_tokens`, `thinking`, provider
  routing), and that provider's router slice (enabled, tier profile, tiers you
  authored, Smart Routing judge target). Install-wide router settings —
  `strategy`, `default_tier`, the Pilot thresholds, judge tuning — stay global
  and are never reverted by a switch. Machine-written tier tables are re-derived
  rather than frozen, so upgrades still move you onto the current recommended
  models. Credentials are never copied into a profile. See
  [docs/configuration.md](docs/configuration.md). (Refs #188)
- Token price charts render inline in Web UI chat. An artifact published as
  `application/vnd.agentos.chart+json` draws as an interactive candlestick chart
  in the transcript instead of a download chip, and both `gmgn-market` and
  `gmgn-token` ship the converter that emits one alongside their text summaries.
  A readout strip above the canvas carries the hovered candle's time, OHLC,
  volume, and close-against-open as a signed percentage at the payload's own
  precision. The chart rides the existing artifact seam, so history replay
  redraws it with no separate path; `lightweight-charts` loads dynamically and
  never enters a chat that has no chart. Payload strings are attacker-controlled
  on-chain metadata and reach the DOM only through `textContent`. See
  [docs/artifacts-and-media.md](docs/artifacts-and-media.md) for the contract a
  skill has to meet to publish one.
- The Web UI has one keyboard shortcut registry and a `?` overlay that lists
  every binding. Components declare a shortcut instead of attaching their own
  document listener, so the editable-target and overlay guards live in one place
  and dialogs register themselves as layers rather than being matched by a
  hardcoded selector list. Combos match on both `e.key` and `e.code`, and the
  New chat tooltip renders the right keycaps per platform instead of a hardcoded
  `⌘⇧O`. The sheet loads lazily. (Closes #137)
- Agent settings → Router Tiers picks tier models from a catalog instead of
  free text. The provider cell is a read-only chip — requests always go through
  `llm.provider`, and save writes it on every tier — while the model cell is a
  combobox over the union of the live `models.list` catalog and the shipped
  `onboarding.catalog.routerProfiles`, so neither an offline install nor a
  provider the gateway has no catalog for produces a false warning. The image
  tier is offered only vision-capable models. Save warns and never blocks, and
  distinguishes an unknown id, an image tier pointed at a model with no vision
  capability, and having no catalog to check against. Context window and price
  per 1M render under the entered model. (Closes #142)

### Documentation

- `features/skills.md` and the tools reference now name the mimes that render
  inline and link to the artifact contract, so a skill author can find out that
  publishing one mime rather than another is the difference between a chart and
  a download chip. Both chart sections say to keep `--output` a bare filename,
  since `publish_artifact` only accepts files under the active workspace.

## [2026.8.6] - 2026-08-06

### Added

- A cron job can run a script instead of a model turn. `--job-kind script`
  makes the script the job: its stdout is delivered verbatim, empty stdout is a
  silent tick, and a non-zero exit delivers the error and fails the job so a
  broken watchdog cannot be mistaken for a quiet one. `--job-kind agent_turn
  --script` runs the script first as a collector — its stdout is prepended to
  the prompt as a `## Script output` block, and a tick that prints nothing (or
  ends with `{"wakeAgent": false}`) skips the turn before the session is
  touched, leaving no session row, transcript line, or model call behind.
  Scripts resolve inside `~/.agentos/scripts/`; absolute paths, `~`, `..`, and
  symlinks out of it are refused, and arguments are exec'd as argv, never handed
  to a shell. Scheduling one requires an interactive CLI or Web caller — the
  in-agent `cron` tool refuses it from a chat channel. (Refs #219)
- New bundled skill `cron-watchers` ships the three script jobs everyone writes
  first — an RSS/Atom feed, a JSON endpoint, and a GitHub repo — each following
  the contract the scheduler expects: print what is new, print nothing when
  nothing is new, exit non-zero on a real failure. Deduplication state lives in
  `~/.agentos/state/cron-watchers/<name>.json`, outside the scripts directory,
  and the first run reports nothing by default. (Refs #219)
- `agentos cron output <job-id> [--run <run-id>]` and the `cron.runOutput` RPC
  read one run's output in full; the in-agent `cron` tool gains
  `action="runs"`, so "what did the watcher report?" is a question the model can
  look up instead of invent.
- `senior-unilp-manager` can open the pool a position lives in.
  `lp_write.py create-pool` initializes hook-less Uniswap v4 pools —
  `hooks` is pinned to `address(0)` with no flag to change it, a dynamic fee is
  refused, an odd fee/tick-spacing pair needs `--allow-odd-tier` because `pools`
  only searches the vanilla tiers, and an already-initialized pool prints its
  poolId and exits without planning. The starting price can never be corrected
  afterwards, so the plan table prints tick, `sqrtPriceX96`, and the price in
  both directions under a banner saying so. selftest goes 695 → 716 assertions.
- `tick --json --alert-only` lets a `senior-unilp-manager` ratchet cron stay
  quiet. A tick that found nothing still prints the whole payload — the run
  history keeps it — but ends on `{"wakeAgent": false}`, which
  `has_actionable_output()` reads as "no news": the run succeeds and nothing is
  delivered. A tick that fired, adopted a landed fire, halted, was rejected,
  expired, or built a plan on a dry run is delivered as usual, and
  `NEEDS_ATTENTION` deliberately alerts on every tick — it is a terminal state,
  so filtering on the action alone would silence the one alarm that must never
  go quiet. (Closes #234)
- `lp_read.py price --tokens` is documented (SKILL.md §6b), with the rule to use
  it rather than deriving a price from a pool — a session derived a token's
  price from a zero-TVL dust pool, was off by 3×, and that number would have
  become the permanent starting tick of a new pool. (Refs #228)

### Fixed

- A bad cron delivery target is rejected at save time instead of failing every
  run. `validate_channel_target` refuses an id beginning with a session-key
  prefix (`agent:`, `cron:`, `webchat:`, `session:`) and requires an integer or
  `@username` for Telegram, suggesting the id that would have worked; `cron.add`
  and `cron.update` then ask the adapter itself via `TelegramChannel.probe_target`,
  where only a definite "no" blocks the save. The new `channels.deliveryTargets`
  RPC lists each channel's paired DMs and configured group chats, so the Web UI
  renders Recipient as a dropdown with `Enter manually…` as the escape hatch.
- A cron run record now carries *why* delivery failed. `DeliveryReport.channel_detail`
  turns one line of "delivery failed" into "delivery to telegram failed: Bad
  Request: chat not found" in `agentos cron runs`, and an exception escaping the
  channel leg reports its type instead of vanishing into `asyncio.gather`.
- `structlog` events reach `~/.agentos/logs/debug.log`. Half this codebase logs
  through `logging.getLogger` and half through structlog, and only the first half
  was written to the file — the missing half included every `delivery.*` warning.
- A cron run's output is stored whole. `clamp_run_output` replaces the scattered
  `[:500]` slices that truncated a script job's stdout on the way into the
  database; `preview_summary` is what the delivery layer and the run list get,
  and the Web UI's expanded row fetches the full text lazily.
- The "→ Chat" button no longer leads to "Could not load chat history." Each run
  reports `chatAvailable` and the button is hidden when it is false — script jobs
  never create a session, and isolated agent sessions are reaped after 24h — while
  `chat.history` answers an empty transcript for a missing cron session instead of
  raising.
- The session reaper is paged. `list_sessions()` returns the 100 most recently
  updated sessions — precisely the ones that are *not* expired — so expired
  isolated cron sessions were never reaped on a busy store.
- A script job's output no longer vanishes. Several skips in the delivery chain
  encode "the run already wrote this into the session", which holds for an agent
  turn but not for a script, which has no turn: a job with `sessionTarget=current`
  from webchat was reported delivered without a byte being written, and a job
  bound to the chat its run *is* was skipped on `origin == session_key`. A
  `cron add --script` from the CLI, which genuinely has nowhere to write, now
  reports `no_session_target` rather than a bare `skipped`.
- A cron tool refusal reaches the model. Cron raises plain `ToolError` in ~30
  places with field-naming messages that `envelope.py` discarded, so a call
  carrying `tool_policy.elevated` on a script job came back as "received an
  invalid argument" followed by seven retries that dropped the required
  `schedule` field. Cron's refusals are `SafeToolError` now, and `job_kind='script'`
  + `tool_policy.elevated` is rejected up front, naming the field. (Refs #228)
- A quoted script path is unwrapped before it is stored. A model passes
  `script='"watch-memory.sh"'` often enough that it is the first thing that
  happens; the job saved cleanly and failed on its first tick. (Refs #219)
- `/reset` clears the visible conversation on web and CLI. `sessions.reset` keeps
  the session key and only rotates `session_id`, so the transcript on screen
  stayed put, which reads as "nothing happened". The Web UI clears on
  `session.epoch_changed`, which covers the typed `/reset`, the slash menu, the
  SessionChip button, and a reset issued by another client; the CLI gains
  `ChatApplication.clear_screen()`, which drops scrollback too.
- Two `senior-unilp-manager` doc commands were unrunnable — `python3 <S>/ratchet.py`
  reads as a redirect from a file named `S` in a shell — and the cron examples
  cannot use `$S` at all, since a cron job runs in a fresh isolated session. Both
  now spell out `{baseDir}/scripts/ratchet.py`. (Refs #228)

### Changed

- The cron surfaces say "no LLM" instead of "no model", and `agentos cron runs`
  grows Delivery and Output columns.

## [2026.8.5] - 2026-08-05

### Added

- The `senior-unilp-manager` skill can run an unattended take-profit ratchet on
  a one-sided Uniswap v4 position. `ratchet.py` arms a mandate that, at fixed
  milestones measured against the **original** principal, exits the position,
  keeps the converted side as realized profit, and redeploys only the
  unconverted remainder into a narrower range running from the current price to
  the original far edge. A fire is a single `modifyLiquidities` — DECREASE →
  BURN → MINT → TAKE_PAIR with no SETTLE leg — so Permit2 is never involved and
  there is no window holding loose tokens and no position; the burned NFT is a
  boolean witness that the fire landed, which makes unattended recovery a lookup
  rather than a guess. Authorization does not go through `--confirm`:
  `MandateAuthorization` is keyword-only, is never passed by `main()`, is
  `isinstance`-checked, and refuses to construct once `_ARGV_ENTRY` is set, so
  the CLI cannot build one. Each fire is re-checked against the pinned chainId,
  PositionManager, poolId, tokenId, signer, recipient, slippage floors,
  milestone index, the fixed far edge, a re-derived near edge, and a zero cap on
  the harvested currency. State lives outside the price cache under
  `$AGENTOS_HOME/state/unilp` as a write-ahead log plus a materialized view,
  fsynced and 0600, with `flock` on a separate file. Arming the same position
  twice is now idempotent for identical terms and refused — naming the differing
  field — otherwise; uniqueness is a scan, because `mandate_id` hashes `label`
  and two labels produced two mandates each intending to burn the same NFT.
  Not yet rehearsed on chain: the combined unlock has never been sent against a
  hooked pool, and SKILL.md marks a dust rehearsal as required before the first
  broadcast.
- The Skills page can pin a run to a skill without retyping its name. Each
  installed skill card carries a `Use` button next to `View details`, and the
  detail dialog gains `Use in chat`; both navigate to Chat with the composer
  pre-filled with `use skill <name>\n`, focused with the caret at the end.
  Nothing is sent and the current chat session is kept — the user writes the
  request underneath. The prefill travels as a one-shot `?prompt=` query param
  that ChatPage reads once on mount and `persistSession` strips, so a reload or
  a shared link does not re-inject it; control characters other than newline are
  dropped and the value is truncated at 2000 characters.
- A cron job's ID is visible in the web UI. It was CLI-only despite being the
  handle every `agentos cron …` command takes; it now renders as the first meta
  row on each card, shortened to head+tail, with a copy button for the full
  UUID.

### Fixed

- Cron cards no longer spill over the neighbouring column. A session key like
  `agent:main:telegram:direct:1245463966:new:59f2` has no break opportunity (a
  colon is not one, per UAX-14), so its min-content width equals its full
  rendered width, and three boxes between the grid track and the text — the
  `MotionListItem` grid item, the `<dl>` row, and the `<dd>` flex item — were
  unable to shrink, widening the card box itself before `overflow: hidden` could
  clip anything. All three get `min-width: 0` and the value truncates with an
  ellipsis, keeping the full string in the DOM and on `title` so it stays
  hoverable and greppable. The prompt row wraps instead
  (`overflow-wrap: anywhere`) so an unbroken contract address does not widen the
  card either.
- A cron job storing an unknown tool profile is now rejected at write time
  instead of dying on every firing. `{"profile": "default"}` — no such profile
  exists; `_TOOL_PROFILES` holds coding, full, memory_only, messaging and
  minimal — stored cleanly and then failed ~50 ms into each run, before the
  agent turn started, until the scheduler auto-paused the job after three
  consecutive errors, with no trace but a run record the operator had no reason
  to look at. `normalize_tool_profile` canonicalises the name or raises listing
  the ones that exist, and `SchedulerOps` plus the `cron` RPC call it before
  storing, mirroring how `elevated` is already handled. The read direction stays
  tolerant on purpose: rows carrying a bad profile already exist, and `cron
  list` has to render them or the broken job could not be found and deleted. The
  tool schema now names the valid profiles, since the model that created this
  job had no way to know them.
- A cron job no longer fails forever once the chat it was created from is
  replaced. The web UI stamps `originSessionKey` onto every reminder job while
  forcing its target to `isolated`, and reminder is the default payload kind for
  a new job — so jobs that never asked to be bound to a session still carried
  one. At fire time the delivery chain mirrors the result into that session, and
  the mirror called `append_message`, which raises `KeyError: Session not found`
  for a session that no longer exists. That surfaced as `forward_failed`, and
  because `best_effort` defaults to off — and its checkbox is only rendered for
  channel and webhook delivery, never for the `none` mode this path runs under —
  the run was marked failed with no way to opt out. Both webchat paths are
  covered: the `none`-mode mirror above, and the `mode=ORIGIN` +
  `channel=webchat` config the cron tool synthesises from the live ToolContext
  whenever the agent schedules something mid-conversation — neither destination
  was picked by the operator, so neither should fail a run that already
  succeeded in its own isolated session. They now report a distinct
  `origin_gone` delivery status. The gateway forwarder looks the session up
  before appending and returns `False` when it is gone; forwarders returning
  `None` keep the previous "delivered" contract, and genuine channel delivery
  failures still fail the run. Channel-created jobs (telegram, discord, slack)
  were never affected: their delivery resolves to the chat, which outlives any
  session.

## [2026.8.3] - 2026-08-03

### Changed

- A `pip install use-agent-os` no longer resolves dependencies open-ended.
  Bounds now cover the rest of the base runtime list — `anyio`, `typer`,
  `rich`, `websockets`, `apscheduler`, `prompt-toolkit`, `questionary`,
  `pillow`, the document stack (`pdfplumber`, `pypdf`, `python-docx`,
  `python-pptx`, `openpyxl`, `reportlab`), the extraction stack
  (`beautifulsoup4`, `readability-lxml`), `yoyo-migrations`, `sqlite-vec`,
  `croniter`, `python-telegram-bot` — and the consumer-facing extras
  (`numpy`, `onnxruntime`, `tokenizers`, `tiktoken`, `jieba`, `mem0ai`,
  `weasyprint`), completing the first seven caps from 2026.7.30. A breaking
  major published in any of them can no longer reach a fresh install on its
  own. Each cap sits at the first release its upstream may break in, measured
  from `uv.lock`: the next major for a `>=1.0` project, and the next **minor**
  for a `0.x` one, where semver puts the breaking change. That distinction is
  load-bearing — `typer<1.0` against a locked 0.24.1 reads as bounded and is
  not, and `weasyprint<70.0` against a locked 68.1 was already letting an
  untested 69.0 into fresh installs. Bounds are targeted rather than blanket:
  `structlog` and `html2text` are CalVer, and `pyyaml`/`jinja2`/`cachetools`
  and peers have long-stable surfaces, so capping those would only make
  AgentOS harder to co-install. The rule and its exemptions are written down
  in CONTRIBUTING.md and enforced by
  `tests/test_packaging/test_pyproject_invariants.py`, which recomputes both
  boundaries from the lockfile — so a new dependency cannot ship unbounded, and
  a cap cannot drift off the rule, by accident. `dev` stays uncapped — it is
  contributor tooling pinned by `uv.lock`, not a consumer surface. (#153)

### Added

- Seven GMGN trading skills ship bundled under the **Trading** category on the
  Skills page: `gmgn-token`, `gmgn-market`, `gmgn-portfolio`, `gmgn-track`,
  `gmgn-holder-analysis` (read-only) plus `gmgn-swap` and `gmgn-cooking`
  (financial execution, `risk: high`). They are vendored from
  https://github.com/GMGNAI/gmgn-skills under MIT and drive the third-party
  `gmgn-cli` npm package, which AgentOS does **not** redistribute: each skill
  declares `requires.bins: [gmgn-cli]` and `requires.env: GMGN_API_KEY`, so
  they list as "Needs setup" with an `npm install -g gmgn-cli` hint until an
  operator installs the CLI and supplies their own key. Gated out of the model
  prompt until then, exactly like `senior-unilp-manager`.

### Fixed

- Shipped Pilot Router tier defaults resolved against static tables that had no
  entry for them, and both tables fail open without logging: pricing falls
  through a `startswith` scan to whatever shorter prefix matches first (or a
  generic $3/$15), the catalog falls through an exact-key miss to a generic
  200K context / 16K output. `glm-4.7-flashx` was estimating at the generic
  default, and seven ids — including `anthropic/claude-opus-5`, the OpenRouter
  c3 default, whose bare spelling was listed at 1M/128K — were sizing turns
  against generic limits. Every tier default now carries an explicit entry in
  both tables, enforced by a test that walks all router tier profiles.
- OpenCAP cost estimates silently used a different gateway's rate sheet after a
  single failed boot fetch. The price cache was seeded exactly once, only when
  the configured provider was OpenCAP, and never refreshed — so one timeout
  meant every estimate for the life of the process came from the shared static
  table, which carries Bankr rates running 3-5x below OpenCAP's own. The cache
  now refreshes on a TTL and refetches when it is cold, mirroring the existing
  OpenRouter live-pricing path, with a shorter negative cache so an unreachable
  catalog costs one bounded attempt rather than one per lookup. When an
  estimate does fall back to the static table it is logged once per model, so a
  substituted number is no longer indistinguishable from a catalog-backed one.
  Set `AGENTOS_OPENCAP_LIVE_PRICING=0` to disable the refresh.
- Asked to install a bundled skill that was only unconfigured, the agent
  searched a community hub instead. A skill declaring `requires` is dropped
  from the prompt until its binary and variables are present, so from inside a
  turn an installed-but-unconfigured skill is indistinguishable from one that
  was never installed — and a same-named catalog row installs into the managed
  layer, which outranks bundled and would have silently replaced the shipped
  skill for every session. Three changes close that path: the installer now
  refuses a first install that would shadow a bundled skill (overridable with
  `force`, and never blocking a reinstall or `agentos skills update` of an
  existing one); `skill_search_community` answers with an `installed_match`
  block, carrying what the local skill is missing and how to fix it, ahead of
  the catalog results; and the `agentos` skill documents both rules.
- `skill_view(name="agentos", section="Skills")` failed even though the skill
  documents skills at length — the material sat under bold labels, which
  `parse_sections` does not index. The six operation groups under **Common
  operations** are real headings now, so each can be read on its own.
- `agentos upgrade` shipped a stale React control UI on any install laid down
  from a local checkout. `scripts/install_source.sh` installs the directory
  itself, so uv's tool receipt records a *directory* requirement; `uv tool
  upgrade` then re-resolved that requirement and rebuilt the wheel from the
  working tree. The wheel bundles `src/agentos/gateway/static/dist/**`, but
  nothing in the upgrade path runs `npm run build` — so every upgrade
  re-packaged whatever browser bundle happened to be on disk. Python code moved
  forward, the web UI did not. `agentos upgrade` now installs the published
  release (`uv tool install --force --python <running> "use-agent-os[recommended]"`
  / `pipx install --force …`), whose wheel carries a control UI built and
  verified in CI. Installing a checkout stays with `scripts/install_source.sh`,
  the only path that rebuilds the bundle first; the command names it when it
  detects a checkout-backed install.
- `agentos upgrade` printed upgrade commands with the extras silently removed —
  Rich parsed the `[recommended]` in `use-agent-os[recommended]` as a markup
  tag and dropped it, so copying the printed command produced an install
  missing the ONNX embedding models and the pilot router.

### Security

- Durable memory ran its own three-pattern redaction rather than the shared
  scanner, so anything the small list missed was written to disk verbatim.
  `redact_memory_text` now goes through `redact_sensitive_text` — the full
  provider-prefix set — and does so with `force=True`, because
  `AGENTOS_REDACT_SECRETS=0` is an *egress* escape hatch and must not unmask
  what lands in durable storage. The keyword rule (`api_key`, `secret`,
  `token`, `password`) handles quoted values and leaves already-masked text
  alone instead of double-redacting it. The shared scanner also learned the
  remaining AWS key-id prefixes (`ASIA` temporary credentials, `ABIA`, `ACCA`)
  and now matches `Authorization` and `x-api-key`-family headers when the name
  or value is quoted — the JSON and dict spellings a tool result actually
  arrives in, which the bare `name: value` patterns walked past.

## [2026.8.2.post1] - 2026-08-02

### Fixed

- The release wheel guard allowed markdown only at a bundled skill's `SKILL.md`
  plus two force-included pptx references, so `senior-unilp-manager`'s
  `assets/v4-reference.md` read as a forbidden entry and the tagged Windows
  release job failed for v2026.8.2 — after the tag had already been pushed.
  `assets/` is where that documentation is supposed to live, and `SKILL.md`
  links it by `{baseDir}`, so stripping it shipped instructions pointing at a
  file that is not on disk. `agentos/skills/bundled/<skill>/assets/**` is now
  allowed; `references/` and stray top-level markdown stay forbidden. A
  real-tree test over the bundled skills fails PR CI instead of the tagged
  release job.
- Cron prompt safety rejected legitimate text: Unicode combining marks (used by
  Vietnamese and many other scripts) were treated as invisible characters and
  blocked. Combining marks are allowed again, while genuinely invisible marks
  stay blocked.

## [2026.8.2] - 2026-08-02

### Added

- A cron job may opt in to running shell-based skills. A cron turn runs under a
  read-only allowlist with `exec_command` hard-denied, so a job that was shown a
  skill could read its `SKILL.md` and never carry it out — nearly every skill
  body is a block of shell. Per-job `tool_policy` could not help, because the
  policy layer re-ORs the hard-denied set at the end and the elevated clamp
  excluded cron outright. The opt-in is stored as `tool_policy["elevated"] =
  "bypass" | "full"` on the column that already persists, so there is no
  migration and it inherits the "channel callers cannot set tool_policy"
  invariant. An opted-in job additionally gets `exec_command`, `write_file` and
  `edit_file`; `cron`, `message`, `agents_list`, subagents,
  `background_process`, `execute_code`, `apply_patch` and `git_commit` stay
  denied, and `"on"` is rejected because it skips the sandbox with no branch in
  the exec approval path. Elevation is refused on non-`agent_run` jobs, since
  the heartbeat loop builds its own read-only context and would silently drop
  it. Surfaced on RPC (top-level `elevated`), the CLI (`--elevated`,
  `--elevated-mode`, `--tool-policy`, plus a list column) and the Web UI (a
  warned toggle and a badge on the job card). Default cron routing is unchanged
  (#184).

- The `senior-unilp-manager` skill can now find and mint into Uniswap v4 pools
  that have **no hook**. It only ever LPs into pools that already exist, and it
  refuses hooked pools unless `--allow-hooked` — so hook-less was always the
  intended default, but on Base it was effectively unreachable: the only fast
  discovery path derived poolIds from a launchpad registry, which by
  construction requires a hook, and Base cannot serve the log scan that would
  find anything else. `pools --token <addr> --no-hook` now derives the poolId
  with `hooks` pinned to the zero address across the conventional fee tiers and
  confirms it in one multicall, so the question "does a plain pool exist for
  this token?" is answered the same way on every chain. `pool`/`mint` accept the
  PoolKey spelled out (`--currency0 --currency1 --fee --tick-spacing`,
  `--hooks` defaulting to none), which skips discovery altogether for any pool
  anywhere; the poolId is recomputed and must match, so a typo errors instead of
  addressing the wrong pool. The skill still never creates a pool.

- `[auxiliary]` configures the model for work AgentOS runs on its own behalf
  rather than as part of a turn — analysing an attached document, describing an
  image. Empty values reuse `[llm]`, so an install that never sets it is
  unchanged; point it at something cheap when those tasks do not need your main
  model. Per-task overrides live in `[auxiliary.tasks.<task>]` (`document` and
  `vision` today), because a text-only model cannot describe an image.

- The system prompt now names the developer tools that actually exist on the
  machine. An agent asked to run the tests reached for `pytest` and found out it
  was missing by running it and reading a shell error, which cost a turn and
  often started a repair for a problem that was never the task. The block is
  probed once per process and lives in the cached part of the prompt, so it is
  paid for once per session rather than once per turn. Only names are emitted,
  never paths. Turn it off with `[prompt] env_probe_enabled = false`.

- `agentos context` shows what every provider request carries before the
  conversation starts. Tool schemas dominate that overhead — about 7,300 tokens
  on a stock install, charged on every call in every turn — and nothing
  surfaced the number, so the only way to learn it was to write a script
  against the registry. The command breaks the cost down, lists the largest
  schemas, and prices each `[tools] profile` against the current one.

- `[tools] profile` is now documented. It already narrowed the tool surface
  sharply — `coding` costs 77% less than `full`, `messaging` 92% less — but it
  appeared in no example config, no doc page and no operator guide, so the
  largest available lever on per-request cost was undiscoverable. Because a
  profile is fixed for the session, narrowing it does not disturb the prompt
  cache.

- A turn that edits code and then answers "done" without running anything is
  now noticed. A passive ledger records which files a turn changed and whether
  a test, build or lint command ran *after* the last change; when the model
  stops on unverified edits the turn emits a warning naming the files and the
  omission. Evidence gathered before an edit does not count for it. Prose,
  data and config files are excluded — a README edit has nothing a test could
  exercise — and messaging surfaces are exempt, since answering a person in
  chat is not maintaining a checkout.

- New bundled skill `senior-unilp-manager`: read and manage Uniswap V4 liquidity
  on Base (8453) and Robinhood Chain (4663). Reads a token's pools with exact
  reserves and per-range market-cap bands, resolves which launchpad deployed a
  token and whether its LP is locked (Clanker v4/v4.1, Liquid Protocol,
  Bankr/Doppler), inspects positions, and mints / increases / decreases /
  collects / burns. Pure Python 3 stdlib over direct JSON-RPC — keccak256, the
  ABI codec, Multicall3, secp256k1 and EIP-1559 assembly are all in-tree, so the
  skill has no dependency beyond a `python3` binary. Every write is a dry run
  that prints a `PLAN_HASH`; broadcasting requires echoing that hash back with
  `--broadcast --confirm`, and the signature is recovered and checked against the
  signer before the transaction is sent. The signing key is read from
  `UNIV4_LP_PRIVATE_KEY` in the environment and never from a command line.
  `python3 scripts/selftest.py` runs 429 offline assertions pinned to golden
  vectors harvested from the reference implementation.

- Skill manifests may declare `metadata.agentos.category`, a subject-matter tag
  distinct from `capabilities` (which describes risk surface, not topic). The
  Skills page uses it to split shipped skills into their own headings, so a new
  crypto skill only has to edit its own frontmatter.

### Changed

- `edit_file` no longer fails on text that differs from the file only in
  formatting. It still tries an exact match first, then falls back through a
  chain of increasingly permissive strategies — indentation, whitespace runs,
  unescaped `\n` literals, smart quotes, and finally block similarity — and
  names the strategy that matched in its result. Text that appears more than
  once is still rejected rather than guessed, now with the line numbers of
  every match, and a failed edit reports the closest regions it found.

- The progress watchdog now sees repeated *successful* calls, not only repeated
  failures. An agent reading the same file over and over produced a clean result
  every time, so by every measure the turn was making progress while burning
  iterations and context on nothing. A call is counted as a repeat only when its
  result is byte-identical to the previous one for the same tool and arguments —
  re-reading a file that changed is real work, and its differing result resets
  the count.

- The Skills page renames two group headings and adds one: `Partners` →
  **Partner Skills**, `Shipped with AgentOS` → **AgentOS Normal Skills**, and a
  new **AgentOS Crypto Skills** between them for bundled skills declaring
  `category: crypto`. Partner skills still win over every other grouping, and a
  local or hub-installed skill cannot move itself under an AgentOS-branded
  heading by declaring the category.

### Fixed

- `senior-unilp-manager`'s confirm gate could be passed with calldata the
  reviewer never saw. The gate spans two processes — a dry run prints a
  `PLAN_HASH`, a human approves it, a second invocation broadcasts with
  `--confirm` — so any flag that reaches the calldata without reaching the hash
  could be swapped in between. Four did: `increase --recipient` fed the sweep
  action while being neither hashed nor displayed, `approve --expiration-days`
  set the Permit2 expiration outside a hash that covered only token and amount,
  `mint --max-tick-drift` could be widened at broadcast to disable the
  re-validation it was approved with, and the deadline *offset* was free to
  change. Every calldata-affecting flag is now bound into the hash. The
  absolute deadline stays out on purpose, so a re-run minutes later still
  matches. New tests mutate one flag at a time across all six subcommands and
  require a different hash, and hold each command to a frozen field set.

- A cron `update` that changed `enabled` alongside other fields discarded the
  rest of the patch. The enabled branch returned right after pause/resume, and
  the Web UI's save always carries `enabled` — so saving a paused job dropped
  its name, text, schedule, timezone and delivery with no error. The transition
  now applies and processing continues, and `enabled` enters the patch so
  `job.enabled` tracks the status, which is what Resume on an auto-disabled job
  needs to take effect.

- The "Set &lt;VAR&gt;" dialog on the Skills page rendered its value field with a
  class that was never defined, so the input fell back to user-agent styling and
  was indistinguishable from the dark panel behind it — the field looked absent.

- Six fixes to the `/control/skills` surface. A wallet-published skill reaches
  the Bankr source only by being named in a wheel-shipped allowlist reviewed in
  this repo, which is the same review path a catalog entry gets, so it now
  groups with Bankr and shows the brand mark on the Installed tab; the provider
  is hardcoded rather than read from the payload, so a hostile registry row
  cannot mint a brand for itself, and the author handle rides along as credit
  and stays searchable. The Bankr and Capminal panels name the prerequisite
  skill each catalog needs, and Capminal infers its category from tags instead
  of hardcoding `crypto`. On status: "No requirements" is gone, since a skill
  with no declared dependencies runs exactly like one whose dependencies are
  satisfied — both are Ready, with the nuance in the tooltip — and Disabled gets
  its own grey bucket instead of showing as "Needs setup" and sending operators
  hunting a dependency that was never missing. A payload without an explicit
  status no longer vanishes from every filter, and a blank or whitespace-only
  env var counts as missing rather than reporting Ready until it fails at
  runtime.

- The packaging test that checks skill docs for links the wheel strips built its
  paths with `str()`, which emits backslashes on Windows, while both things it
  compares against spell those paths with forward slashes. Nothing matched on
  `windows-latest`: force-included files were never skipped and every known
  stranded reference read as newly stranded. Paths are compared as posix now.

- `senior-unilp-manager` sent an agent round in circles on "add N of my token as LP
  from the current price up to a market cap of X" — one real run spent twelve minutes
  and forty commands without reaching a mint. Several things compounded, all fixed
  here. **§8 documented the single-sided rule backwards** (it claimed a range above the
  current price takes `currency1`; it takes `currency0`), so the agent picked the wrong
  side, was refused, assumed it had the wrong pool, and went looking for another one.
  **A hook was treated as disqualifying**: the docs only noted that Clanker/Liquid
  reject third-party LPs, so the Doppler pool — the only one with real depth — was
  abandoned for dust pools at 85% fee tiers; §8 now gives the per-hook policy and says
  to read the decoded `BEFORE_ADD_LIQUIDITY` flag. **`ticks` snapped outward
  unconditionally**, so a band starting at today's price always straddled the current
  tick and came back two-sided; `--from-current` now pulls the near edge onto one side,
  keeping the larger part of the band. `--mcap-lower` / `--mcap-upper` may be given in
  either order rather than erroring, since which is larger depends on where the target
  sits. `pools` ends with a `recommended pool` block — deepest by TVL, id in full,
  whether `--allow-hooked` is needed, and the next command. Part B opens with a
  five-command recipe covering the `approve` step, which the agent kept skipping.

- `senior-unilp-manager` lost USD prices to rate limiting far too easily, and `ticks`
  turned that into a hard failure. GeckoTerminal's free endpoint refuses after a couple
  of calls in quick succession, and since every command is a fresh process the
  in-memory cache never helped — running `pools` and then `ticks` on what it found was
  enough to trip it. Prices are now cached to a short-lived file shared across
  processes, and a throttled lookup says it is temporary and worth retrying instead of
  reporting "no USD price", which read as a property of the token and sent callers off
  to look at other pools.

- A turn could die with `TypeError: 'NoneType' object is not iterable` partway
  through a streaming reply, taking the whole answer with it. The OpenAI-compat
  stream reader read `choices`, `delta`, `tool_calls` and each tool call's
  `function` with a `dict.get(key, default)` — but that default only applies
  when the key is *absent*, and several gateways send the key with an explicit
  `null` instead of omitting it (a text-only delta carrying `"tool_calls":
  null`, or a usage-only final chunk carrying `"choices": null`). Those reads
  now treat null and missing identically, so a chunk shaped that way is skipped
  rather than crashing the turn. The non-streaming path read `choices` the same
  way and is fixed alongside it.

- Tool schemas from MCP servers went to the provider exactly as the server
  emitted them, so one malformed tool could fail the whole request and take
  every other tool down with it. Schemas are now normalized once at discovery:
  `$ref` into `$defs` is inlined, `anyOf`/`oneOf` unions that exist only to
  permit `null` collapse to their concrete branch, `"type": ["string", "null"]`
  becomes `"string"`, objects without `properties` gain an empty one, and
  values that are not schemas at all are dropped along with any `required`
  entry naming them.

- Side-task LLM calls spent tokens that nothing recorded. Analysing a document
  and describing an image each built their own provider client, so the cost
  appeared on the provider bill but never in `agentos cost`. Those calls now run
  through one auxiliary client that bills the session that triggered them and
  additionally records them under an `aux:<task>` scope, keeping runtime cost
  separable from turn cost. Two copies of the provider-to-credential mapping in
  `tools/builtin/media.py` collapse into one — the document path had only ever
  read the environment, so it ignored a key configured in `[llm]` for the same
  provider and now finds it.

- A side task with no reachable API key sent the request anyway and failed on
  `Illegal header value b'Bearer '`, which named neither the provider nor the
  variable to set. It now fails before the request with both. Local backends
  such as Ollama, which authenticate by reachability rather than by key, are
  unaffected.

- A failing provider request read its error body whole. That body is written by
  whatever sits in front of the provider — a WAF's HTML block page, a proxy's
  stack trace — and has no size contract, so the read was unbounded; on the
  Anthropic path the entire decoded body then became the `ErrorEvent` message
  and flowed into the agent's context. Error bodies are now read to a bound and
  summarised: JSON keeps its `error.message`, an HTML page collapses to its
  title and size, and anything else is truncated with the cut made visible.

- `skill_view` now resolves `{baseDir}` to the skill's install directory and
  opens every read with a `[Skill directory: ...]` line. The placeholder is how
  every bundled skill names its own scripts, but nothing had ever expanded it and
  nothing else in a session revealed where a skill lived — so an agent handed
  `python3 {baseDir}/scripts/lp_read.py` looked for that path under the
  workspace, found nothing, and reported the skill as not installed. Expansion
  happens at render time, not at load: `skill_edit` writes `SkillSpec.content`
  back to disk, and expanding earlier would bake one machine's absolute path
  into a shipped `SKILL.md`.

- The Skill dialog rendered `[object Object]` for every declared environment
  variable. `_requirements_item` put `SkillRequires.env` on the wire, which is a
  list of `SkillEnvVar` dataclasses, instead of `env_names`. No bundled skill had
  declared `requires.env` before now, so nothing had hit it.

## [2026.7.31] - 2026-07-31

### Changed

- Onboarding router tier defaults move up a generation across all three
  gateway profiles (`openrouter`, `bankr`, `opencap`): C1 goes from
  `minimax-m3` to `gpt-5.6-luna` and C3 from `claude-opus-4.8` to
  `claude-opus-5`. On the OpenRouter profile C0 moves to
  `deepseek/deepseek-v4-flash` so C0 and C1 do not collapse onto the same
  model and the cheap tier keeps its purpose. `claude-opus-5` is registered
  in the model catalog and pricing tables, so its context window is 1M rather
  than the 200K default and the usage tracker reports real cost.
  `minimax-m3` stays as the `image_model` vision route and is deliberately
  left out of the migration maps, which apply to every tier including
  `image_model` (#169).

- The Pilot Router docs now describe the C0–C3 tiers — what each tier is for,
  which model each gateway profile assigns to it, and how to pick one — and
  the OpenCAP routing page no longer mentions `oc-uncensored-1.0` (#170).

### Fixed

- Any skill that called an authenticated HTTP API was dead on arrival. The
  outbound guard matched credential-ish *names*, so `http_request` refused
  every `Authorization` and `x-api-key` header, and `exec_command` refused
  `{"sellToken": …}` (a web3 asset, not a token), `grep "token: "`, and
  `CAP_API_KEY=$(jq -r …)` — while a real key pasted inline passed through.
  With no working call path and no approval route, the model routed around it
  by writing the key to a file and running that, which the guard never
  inspected (#165).

  The guard now matches credential **values** — a PEM block, a
  vendor-prefixed provider key, a DSN password, an `/etc/passwd` line — and
  leaves names alone. An opaque API key in a header is how authenticated APIs
  work and is no longer refused. The shell check runs only on commands that
  can reach the network, mirroring the gate `execute_code` already applied.
  Blocks now name a working alternative instead of dead-ending, and
  `AGENTOS_SENSITIVE_PAYLOAD_DISABLED=1` turns the check off.

  What replaces the pattern match is a credential path: a skill declares
  `metadata.requires.env`, and those names — and only those — are forwarded
  into `execute_code`'s sandbox for the session that loaded the skill, so the
  value never enters the transcript. A skill AgentOS did not ship cannot
  declare one of AgentOS's own provider keys.

### Added

- Command output is scanned for credentials before it reaches the model.
  `exec_command`, `background_process` and `process(action=log)` mask
  vendor-shaped keys, auth headers, JWTs, private keys and DSN passwords.
  File content gets a non-reusable sentinel rather than a head/tail mask, so
  an agent that reads a key and writes it back cannot silently corrupt it.
  `AGENTOS_REDACT_SECRETS=0` disables it; the value is read once at startup so
  a command cannot switch it off mid-session.

- `AGENTOS_STRIP_PROVIDER_ENV=1` withholds AgentOS's provider credentials from
  child processes. Off by default because bundled skills read those names from
  `os.environ`.

### Security

- `AGENTOS_GATEWAY_TOKEN` and the sandbox guard switches no longer reach
  child processes. Every `exec_command` previously inherited `os.environ`
  verbatim, including the token that authenticates to the control plane.

- `http_request` now refuses cloud metadata endpoints (`169.254.169.254`,
  `metadata.google.internal`, ECS task credentials). The repo already shipped
  an SSRF guard and `web_fetch` used it, but `http_request` validated only the
  URL scheme. Ordinary private addresses stay reachable — unlike `web_fetch`,
  this is the tool people point at a local dev server on purpose.

## [2026.7.30] - 2026-07-30

### Added

- Native support for Capminal Skills (`Capminal/agent-skills`) in the Skills
  hub: browse, inspect, and install allowlisted Capminal skills with publisher
  branding, carried by a Capminal brand mark that falls back cleanly when the
  logo cannot be fetched (#144).

### Changed

- Runtime dependencies in `pyproject.toml` now carry upper bounds, so a major
  release of a dependency cannot land in an install that was resolved against
  the previous one (#153).

### Fixed

- The agent could not tell which of its installed skills applied to a request,
  and answered from general knowledge instead. Four separate causes, each of
  which alone was enough to produce that:

  - The prompt budget was a cliff. One skill over it and *every* description in
    the block was dropped for a name-only list — a 27k render fell to 3.5k
    against a 24k budget, so 20k of the configured allowance bought nothing and
    the model had never seen a single description. Descriptions are now
    shortened to the longest length that fits (the widest, not the first that
    works: 451 chars where a fixed step would have settled for 320), and skills
    are only dropped once even a names-only list overruns. A default install
    was unaffected; the cliff was reached by installing skills, which is
    backwards.
  - Names-only mode told the model to call `skill_view` on every entry that
    might be relevant — up to one call per skill, which no model will do — and
    never mentioned `skill_list`, which returns every description in one call.
    It now points at `skill_list`, and only when the session actually has it.
  - With prompt caching on (the default), the block was delivered as a *user*
    message headed "not a user request … use it only when it is relevant",
    contradicting its own "read this before answering" from the weakest
    position in the request. When the skill list is the same every turn
    (relevance filtering off, the default) it now belongs to the cacheable
    system prompt instead, which is also where it is cheapest.
  - Scheduled (cron) turns received the block but not `skill_view` or
    `skill_list`, so following it was impossible. Both tools — read-only — are
    now on the cron allowlist.

- Asking for a skill that is not installed read as a broken tool. `skill_view`
  answered with what *not* to do and "tell the user the skill is not installed",
  offering no next step even when a configured hub carries the skill and the
  tools to fetch it are in the session — so a model dressed the dead end up as a
  failure, reporting that `skill_view` "returned error: 14", a code that exists
  nowhere in AgentOS. It now says the lookup worked and the skill simply is not
  here, names installed skills with similar names, and points at
  `skill_search_community` — only when the session can actually reach it, and
  always as an offer to install rather than an instruction to (#162).

- Reading a large skill cost the whole skill. `skill_view` returned every byte
  of a SKILL.md, and unlike the system prompt a tool result is not cached, so a
  56 000-character hub skill spent ~14 000 tokens per read and again on every
  re-read — the shape behind reports of skill loading being slow and expensive.
  Over `[skills].max_skill_view_chars` (new, default 10 000) it now returns the
  skill's opening sections plus an index of the rest, read on with
  `skill_view(name, section="<title>")`. Across the skills on a real install
  that is 43% fewer characters, and 80–87% on the largest. Shipped skills are
  unaffected: the largest is 21 600 characters and the median 2 400. A body with
  no headings, or one only slightly over the ceiling where the index would cost
  more than it saves, is still returned whole.

- A skills block that quietly lost its descriptions was indistinguishable from
  an operator uninstalling skills: nothing was reported as dropped and the
  character count simply fell. Each turn now records `skills_render_mode` and
  `skills_description_max_chars` in the decision log, and any render below
  `full` is logged as `skills_filter.budget_degraded` with what to change.

- A skill's linked files were named with the platform separator, so on Windows
  the index offered `references\api.md`. A model quotes that back as
  `file_path`, where the backslash is a JSON escape, and it matches neither the
  tool call nor how a `SKILL.md` writes its own links. Paths are emitted as
  POSIX now. Nothing was unreadable — the reader already normalised separators;
  the defect was in what the agent was told to type.

- Skill cards in the Web UI overflowed their grid tracks and changed height
  with their content, so the hub grid reflowed as cards loaded. The card layout
  is fixed now and long text is contained rather than pushing the track wider
  (#135, #161).

- The **Installed** chip in the skill detail dialog did not reflect whether the
  skill was actually installed (#121).

## [2026.7.29] - 2026-07-29

### Added

- Bankr skills published from bankr.bot — the ones that live under an author's
  wallet address instead of in the `BankrBot/skills` repository — can now be
  browsed and installed like any other hub skill, starting with
  `stock-premium-lp-manager`. They arrive as JSON with the body inline, so the
  `SKILL.md` is synthesized from the payload rather than downloaded from a
  repository, and the skill is credited to its author rather than inheriting
  Bankr's brand. As with the repository half, only allowlisted skills can be
  installed through the Bankr source, so it cannot be used to pull an arbitrary
  author's skill and record it as having come from Bankr's hub.

### Security

- `SECURITY.md` now answers what happens to an audit report: findings go
  through the private advisory form rather than a pull request adding an audit
  document to the repository, there is no bug bounty program, and a researcher
  whose report leads to a fix is credited in that fix's release notes (#154).

## [2026.7.28] - 2026-07-28

### Added

- A new chat can be started from anywhere in the console with
  `Cmd/Ctrl+Shift+O`, using the same flow as the New Chat button. The button
  tooltip shows the platform-appropriate hint (#131, closes #120).

### Fixed

- The settings screen called itself three different things depending on where
  you looked. The route title, sidebar item, page heading, browser tab title,
  and the docs now all say **Agent Setup** (#125, closes #123).

## [2026.7.27] - 2026-07-27

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
- "Is the agent actually being offered this skill?" is now a question with an
  answer. Every skill row from the gateway, and every line the agent's own
  skill listing prints, carries whether the skill is offered and — when it is
  not — which of six reasons applies: model invocation is disabled in its
  manifest, a requirement is missing, a tool it needs is not enabled in this
  session, a native tool supersedes it as a fallback, relevance filtering
  skipped it for this message, or the injected skills block was full. The
  explanation is one sentence naming what to do, and it never contains a
  filesystem path. Ready and offered were previously the same green dot, which
  is why a perfectly installed skill could sit there being silently withheld.
  Five of the six answer from the installed set alone, so the Skills page shows
  them before you send anything; only the relevance-filtering one needs a
  message to rank against and stays in the decision log.
- With `[tools] enabled = false`, skills that require a tool are now reported as
  withheld rather than available. The Skills page previously answered against
  everything the install could offer while chat answered against a turn with no
  tools at all — the same skill, two answers.
- How a skill was acquired — `shipped` with AgentOS, installed from a `hub`, or
  a `local` directory you added — is now a fact AgentOS records and reports,
  alongside the source, identifier, version, and install time for hub installs.
  It is derived from the install record rather than guessed from which
  directory the files sit in, so moving a skill does not change the story of
  where it came from.
- The same record answers whether Update and Remove will actually work. A
  hub-installed skill whose files no longer sit where the lockfile recorded
  them keeps Update — an update re-fetches by identifier — and loses Remove,
  because AgentOS will not delete files it cannot prove it owns. The Web UI
  says so instead of offering a button that fails.
- Skills can name a publisher, so a partner's skills carry that partner's
  identity whether they shipped with AgentOS or you installed them from that
  partner's hub. Publishers are allowlisted **inside AgentOS**: a `SKILL.md` or
  a hub catalog can only *select* a recognized publisher by id, never describe
  one. A third-party skill that writes a partner's name, URL, and logo into its
  own frontmatter renders as an ordinary unbranded skill. Selecting an id is
  restricted too: only a skill shipping inside the release may name its own
  publisher, and an installed one is branded by the hub catalog row it came
  from, so a directory dropped into a skills path can never appear as a
  partner. Publisher is independent of provenance — one says whose name is on a
  skill, the other where the text came from and under what licence.
- `agentos skills list --json` gained `publisher` and `acquisition`, built by
  the same code the gateway and the Web UI use. It deliberately has no
  `availability` key: that depends on a chat session's tool surface, which a
  CLI process does not have, and an absent key means "not computed" rather than
  "not offered".

### Changed

- The Skills screen's Installed tab now groups cards by where a skill came
  from — **Partners**, **Shipped with AgentOS**, **Installed from a hub**,
  **Your local skills** — instead of by which directory holds the files. The
  storage layer is still shown, as a chip on each card, because it decides
  which skill wins a name collision; it no longer decides the heading. If you
  navigated by the old `Bundled` / `Managed` headings, the cards under them are
  now under `Shipped with AgentOS` and `Installed from a hub`.
- `skills.max_skills_prompt_chars` now defaults to **24000**, up from 8000. The
  bundled skill set renders to about 16k characters with descriptions, so the
  old default silently forced every default install past the budget and into
  name-only mode — the model had never seen a skill description on a stock
  install. Raise it further if you install many skills; lower it if you run a
  model with a small context window, where the whole-request ceiling can be
  smaller than this budget. See
  [configuration.md](docs/configuration.md#skill-prompt-budget).

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
- The skills block no longer writes an absolute filesystem path for every
  skill into the system prompt. Nothing read it — skills are looked up by name,
  and the skill-reading tool explicitly tells the model not to go looking on
  disk — while it accounted for roughly two thirds of the block and put the
  user's home directory in front of the model on every turn. Removing it, with
  the raised budget above, is what lets a stock install list every skill with
  its description.
- Upgrading lifts an existing `skills.max_skills_prompt_chars = 8000` to the new
  default. The key is materialised into every saved `config.toml`, so raising the
  default alone would have reached new installs only, and 8000 is exactly the
  value that cannot fit the shipped skills' descriptions. The rewrite runs with
  the config migrations on the next gateway start, takes the usual timestamped
  backup, and touches only that exact old default — a budget someone chose is
  left alone.
- A skill that appears in a skills directory while the gateway is running is
  now picked up on the next turn instead of at the next restart. The cache was
  only cleared through AgentOS's own install paths, but the directories are
  shared: `agentos skills install` runs in a separate process, and
  `~/.agents/skills` is written to by other agents on the same machine. A skill
  from either was on disk, absent from `skills.list`, absent from the prompt,
  and unmentioned in any log. The cache is now validated against the same
  file manifest the on-disk snapshot already used, which costs one stat sweep —
  measured at 0.6 ms for 65 skills — on each load.
- `~/.agents/skills` and `<project>/.agents/skills` are honoured when they are
  created after startup. Both resolved once at boot and a missing one collapsed
  to "no such layer", so the first cross-agent install on a machine stayed
  invisible until a restart. The managed directory was already exempt for this
  exact reason; these two now match it.
- The guidance above the skills list no longer argues against using it. It
  opened with "Skills are optional task playbooks" and told the model to load
  one "only when a listed entry clearly matches" — while the same block, in the
  compact mode a stock install always fell into, listed nothing but names. A
  bare name matches nothing clearly, so the instruction could not be followed
  and the honest reading was "skip it". The two failure modes are not
  symmetric: loading a skill that turned out to be unnecessary costs a little
  context, and skipping one that carried the right endpoints, commands, or
  conventions produces a confidently wrong answer. The block now says so, asks
  the model to load on partial relevance, and — when only names are listed —
  states plainly that a name is not enough to rule a skill out.
- When the skills block does overflow its budget, the skills it drops are no
  longer chosen by load order, which always landed the cut on the skills an
  operator had installed. The cut now follows layer precedence, so `extra` and
  then `bundled` skills go before the ones in a writable skills path. The drop is
  also reported instead of being silent: a `skills_filter.budget_truncated`
  warning naming the dropped skills, and a `prompt_budget` reason on each
  affected skill.
- The skill count and skill-id list in turn metadata and the
  `skills_filter.applied` log counted skills that the budget had already thrown
  away, so the one place that could have revealed the problem asserted
  everything was fine.
- Setting an environment variable or installing a missing binary now takes
  effect for the agent without a gateway restart, which is what the
  Environment feature promised. The chat path built one eligibility cache at
  import time and remembered a *negative* lookup for the life of the process,
  while every other surface rebuilt its own per call — so the Skills screen
  reported a skill as ready while the agent refused to be given it, forever.
- Browsing or searching a skill hub now also shows skills you already installed
  from that source, including ones its catalog does not list. A skill installed
  from a GitHub URL used to vanish from the page it was installed on, because
  an empty browse never returned a row for it.
- The Installed marker in the community list no longer goes stale after a
  removal, and no longer fires on a catalog entry whose *name* happens to match
  an unrelated skill's install *identifier*. Names are matched against
  installed names and identifiers against installed identifiers.
- Installing a skill while a search is open no longer loses the row when the
  search is cleared, and the skill dialog no longer unmounts itself when the
  list underneath it changes.
- A failed hub search reports the failure instead of rendering as "no skills
  match your query", and results the hub matched on a tag are no longer
  discarded by a second client-side filter that could not see the tag.

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
