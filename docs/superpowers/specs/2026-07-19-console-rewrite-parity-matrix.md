# Console Rewrite Parity Matrix

> **Plan 1 complete — 2026-07-20.** Layer-0/1 foundation (bootstrap, WS-RPC,
> theme, AppShell + 13 routes, health view) ported and verified: FE gate green
> (77 unit tests after this parity-fix round grew the covering suites, `vite
> build` clean), Python gate green (ruff + mypy clean,
> 1559 gateway/parity tests passing), legacy UI byte-identical (empty
> `git diff` on static js/css/vendor + templates). Every cross-cutting row is
> `ported` except two cutover-plan items (`tokenViz` feature flag, custom
> `base_path`) — the theme-flash inline script and noscript message ride in the
> new SPA shell `frontend/index.html` and are `ported`, with only their
> served-page wiring folded into cutover; the `### health` section has
> zero functional `pending` rows (one owner-sign-off `waived` delta at cutover,
> one live-parity row folded into cutover). Remaining `pending` rows and the 12
> unfilled view sections belong to Plan 2+.

Single source of truth for migration completeness (spec §6). A behavior row
may be `pending`, `ported` (with evidence: test name or verification note),
or `waived` (with reason, owner-approved at cutover).

Row format:
| behavior | legacy source | status | evidence / reason |

## Cross-cutting
| behavior | legacy source | status | evidence / reason |
| --- | --- | --- | --- |
| Theme persistence + system-default resolution | js/theme.js:8-38 | ported | theme.test.ts::theme store > initTheme applies stored preference; initTheme resolves the system default (dark) when nothing is stored; initTheme resolves the system default (light) when nothing is stored (both mock matchMedia '(prefers-color-scheme: dark)'); initTheme prefers a stored value over the system default; set persists and applies; toggle flips the mode; rejects invalid modes |
| Theme flash prevention inline script | templates/index.html (head) | ported | frontend/index.html head `<script>` (lines 13-27) runs before CSS: reads localStorage['agentos-theme'], falls back to matchMedia('(prefers-color-scheme: dark)') when unset/invalid, sets `data-theme` on documentElement, catches storage errors → 'light' — 1:1 with legacy templates/index.html:12-24. Served as the SPA shell `<head>`; wiring the built shell as the rendered page is the cutover-plan item, the inline-script behavior itself is ported |
| Favicon links: icon + shortcut icon + apple-touch-icon → agentos-mark.png | templates/index.html (head favicon block) | ported | frontend/index.html head — three link tags reference /src/assets/agentos-mark.png (png copied from static/img); Vite fingerprints the asset and rewrites the href under base_path at build (base '/control/static/dist/'), mirroring legacy's base_path-prefixed hrefs |
| WS handshake: connect.challenge -> connect(protocol 3) -> HelloOk+policy | js/rpc.js:87-127 | ported | ws-rpc.test.ts::handshake > answers connect.challenge with a protocol-3 connect request incl. auth token; enters connected state and stores policy on HelloOk |
| WS req/res correlation + typed errors (code/details) | js/rpc.js:45-147 | ported | ws-rpc.test.ts::call correlation > resolves with payload on ok res, matching by id; rejects with RpcError carrying code and details; rejects immediately when not connected; rejects all pending calls when the socket closes |
| WS event fan-out incl. wildcard '*' listener | js/rpc.js:148-154 | ported | ws-rpc.test.ts::events > fans out to named and wildcard listeners with meta |
| WS seq-gap detection -> close+reconnect (_gap) | js/rpc.js:188-202 | ported | ws-rpc.test.ts::events > detects a seq gap, emits _gap, and closes the socket |
| WS tick-watch (policy.tick_interval_ms, 2.5x timeout) | js/rpc.js:204-217 | ported | ws-rpc.test.ts::keepalive and reconnect > closes the socket when no frame arrives within the tick timeout |
| WS keepalive ping every 55s | js/rpc.js:172-179 | ported | ws-rpc.test.ts::keepalive and reconnect > sends a ping every 55s while open |
| WS reconnect backoff 800ms x1.7 max 15s | js/rpc.js:226-231 | ported | ws-rpc.test.ts::keepalive and reconnect > reconnects with backoff after close (800ms first retry) |
| Default route: /overview desktop, /chat on <=768px | js/router.js:32 | ported | app/routes.tsx::defaultViewPath + IndexView (index route renders the default view in place); matchMedia('(max-width: 768px)') → chat else overview; AppShell.test.tsx::index route renders the default view without changing the URL > renders Overview on desktop…; renders Chat on mobile… |
| Index route renders the default view WITHOUT rewriting the URL, and highlights the default view's nav item | js/router.js:29-66 | ported | app/routes.tsx IndexView renders the default view's element at the base path (no Navigate/replace, address bar stays at base); AppShell.tsx activePath forces the default nav item .is-active + aria-current="page" at the index; AppShell.test.tsx::renders Overview on desktop and leaves the URL at "/" (asserts router pathname "/" + aria-current on the Overview link) |
| ^ (was) delta: matchMedia evaluated once at module load vs legacy per-navigation | routes.tsx defaultViewPath vs js/router.js:32 | resolved | RESTORED to per-resolve: defaultViewPath() reads matchMedia inside IndexView render (and AppShell activePath), not once at module load — matches legacy's per-`_resolve()` evaluation; earlier waiver retired |
| 404 route fallback rendered as text (XSS-safe) | js/router.js:48-55 | ported | app/routes.tsx::NotFound reads useLocation().pathname (router-driven — createMemoryRouter never updates window.location, so the earlier window.location read made the XSS assertion vacuous) and renders it as JSX text; AppShell.test.tsx::routes > renders XSS-safe 404 text for unknown paths now routes to '/nope<script>alert(1)</script>' and asserts the full hostile string is present as literal text AND no <script> element was injected |
| ^ (was) delta: new 404 renders full pathname (/control/nope) vs legacy basename-relative (/nope) | routes.tsx NotFound vs js/router.js:54 | resolved | RESTORED to basename-relative: NotFound reads useLocation().pathname, which is basename-relative under react-router (main.tsx sets basename from BASE_URL), so it shows '/nope' like legacy's `rel` — not the full '/control/nope'; earlier waiver superseded |
| Document title per route ("<Title> - AgentOS Control"; unmatched route -> "Not Found - AgentOS Control") | js/router.js:68-71 | ported | AppShell.test.tsx::routes > sets the document title from the route (views/StubView.tsx useEffect); sets the 404 document title to "Not Found - AgentOS Control" (routes.tsx NotFound useEffect — an unmatched route has no meta.title so legacy resolves to 'Not Found', router.js:68) |
| Nav active state + aria-current | js/router.js:59-66 | ported | app/AppShell.tsx computes activePath (current pathname segment, or defaultViewPath() at the index) and sets .is-active styling + aria-current="page" on the matching Link — including the index case where no URL segment exists; AppShell.test.tsx::renders Overview on desktop… / renders Chat on mobile… assert aria-current on the default nav item |
| Bootstrap data: version/ws_url/auth_mode/base_path/config_path/features | control_ui.py:_build_bootstrap_context | ported | test_control_ui_bootstrap.py::test_bootstrap_returns_json_context |
| Bootstrap consumption: fetch /api/bootstrap, connect WS (stored wsUrl/wsToken override), mirror _state into connection store | js/app.js (bootstrap fetch + ws connect) | ported | lib/bootstrap.ts + app/providers.tsx; the fetch→WS-connect path is exercised directly (not just indirectly via view tests): providers.test.tsx stubs fetch and asserts rpc.connect args on the resolved bootstrap — ::reads the auth token from sessionStorage, not localStorage; connects without a token when sessionStorage has none; prefers the stored wsUrl override over bootstrap ws_url. _state mirroring into the connection store is wired via rpc.on('_state', …) in providers.tsx |
| ^ delta: legacy inlined bootstrap in the HTML (could not fail); new fetch of /api/bootstrap can. On failure the shell still renders and connects with the location-derived default WS URL (fallbackBootstrap mirrors app.js:186-203 + getDefaultRpcUrl) | js/app.js:186-203 | ported | providers.test.tsx::renders the shell and connects with the location default when bootstrap fetch rejects; …when bootstrap responds non-ok; lib/bootstrap.ts fallbackBootstrap/defaultWsUrl |
| Stored WS override wins over bootstrap ws_url (agentos.wsUrl / agentos.wsToken) | js/app.js:197-203 | ported | app/providers.tsx — URL override read from localStorage['agentos.wsUrl'] (used verbatim, ahead of resolveWsUrl), auth token read from sessionStorage['agentos.wsToken'] (legacy per-tab tier, app.js:201/:212), both try/catch-guarded; providers.test.tsx::reads the auth token from sessionStorage, not localStorage; connects without a token when sessionStorage has none; prefers the stored wsUrl override |
| ^ delta: default WS scheme. Legacy always derived the scheme from location.protocol (wss:// on https, app.js:191-195); the new console prefers the server-computed bootstrap ws_url. Behind a TLS-terminating proxy that omits x-forwarded-proto the server emits ws:// for an https page — a mixed-content downgrade the browser blocks. RESTORED to legacy: resolveWsUrl (bootstrap.ts) prefers the location-derived wss:// default when the page is https and ws_url is a same-host ws:// downgrade; a different host, an already-wss ws_url, or a non-https page passes through untouched. Stored overrides are unaffected (used verbatim). | js/app.js:191-195 getDefaultRpcUrl | ported | lib/bootstrap.ts resolveWsUrl; bootstrap.test.ts::resolveWsUrl (same-host downgrade → wss; wss passthrough; cross-host ws left alone; http page untouched; unparseable raw); providers.test.tsx::connects with the location-derived wss default when bootstrap ws_url downgrades a same-host https page to ws:// |
| Mobile sidebar drawer (<=768px): hamburger toggle, close on nav-click / outside-click / Escape, aria-expanded + aria-hidden/inert sync | js/app.js:119-171 | ported | AppShell.test.tsx::mobile sidebar drawer > hides the drawer on mobile until the hamburger opens it, and closes on nav click; closes on Escape and on outside click; keeps the sidebar visible to AT on desktop; app/AppShell.tsx (matchMedia 768px + addListener fallback, document click/keydown listeners, inert/aria-hidden when hidden drawer) |
| Connection indicator: PERSISTENT pill (never unmounts), variant ok/warn/err + compact 'Connected' ok state, capitalized label as text + title attr, role=status aria-live=polite | js/app.js:94,174-183 | ported | AppShell.tsx renders a persistent #conn-pill in topbar-left across all states (connected→ok, connecting→warn, disconnected→err; label = capitalized state; title=label); AppShell.test.tsx::shows a persistent connection pill across all states including Connected (asserts the pill STAYS mounted with the ok/'Connected' state, title, and data-variant on connect — legacy did not unmount the indicator) |
| Sidebar version footer: 'v<semver>' from bootstrap.version with build-suffix (+NNN) stripped + safe-charset filtered (max 32 chars), suppressed entirely when empty | js/app.js:58-68 (_buildLayout version footer) | ported | AppShell.tsx sidebarVersion() (split('+')[0], /[^0-9A-Za-z.\-]/ filter, slice(0,32)) + nav-foot block reading useBootstrap().version; AppShell.test.tsx::renders the sidebar version footer with the build-suffix stripped (2026.7.19+1779915602 → v2026.7.19); suppresses the version footer when the bootstrap version is empty |
| Sidebar information architecture: nav grouped under labels Chat / Control / Settings, Chat first, Approvals last under Settings; item order within groups matches legacy markup | js/app.js:72-88 (_buildLayout nav structure) | ported | AppShell.tsx NAV_GROUPS (Chat: chat; Control: overview,health,channels,skills,sessions,agents,usage,cron; Settings: setup,config,logs,approvals); AppShell.test.tsx::groups nav under Chat / Control / Settings with Chat first and Approvals last (asserts label order + first link Chat / last link Approvals) |
| noscript message | templates/index.html | ported | frontend/index.html body `<noscript>` (lines 30-35): "JavaScript required" heading + the "needs JavaScript to render the chat, sessions, and configuration views" sentence, mirroring legacy templates/index.html:47-52 (new drops the legacy inline light-only border/color styling, which is theme-polish deferred to the styling pass, not a behavior). Served in the SPA shell; cutover wires the built shell as the rendered page |
| Feature flag AGENTOS_FEATURES.tokenViz (default false) | js/app.js:6-9 | pending | |
| Custom base_path support for built assets | control_ui.py + vite base | pending | cutover-plan item |

## Views
(One section per view; filled by each view's Task before implementation.
 Health is filled in this plan; the other 12 in later plans.)

### health
| behavior | legacy source | status | evidence / reason |
| --- | --- | --- | --- |
| doctor.status RPC {agentId:'main', deep:true} after waitForConnection | health.js:76-77 | ported | HealthPage.test.tsx::HealthPage > calls doctor.status deep for agent main and renders grouped findings (asserts call('doctor.status', {agentId:'main', deep:true}) after waitForConnection) |
| doctor.status call lifecycle: exactly one deep call per view entry, error rendered immediately (no retry), fresh load on re-entry, no background refetch on tab focus / reconnect | health.js:64-77 | ported | HealthPage.tsx useQuery pinned to legacy (retry:false, staleTime:0, gcTime:0, refetchOnMount:'always', refetchOnWindowFocus/Reconnect:false — overrides providers.tsx defaults staleTime 5s / retry 1); HealthPage.test.tsx::renders the error immediately without retrying; reloads fresh on every view entry instead of serving a cached report |
| Loading state: "Checking readiness" + loading strip, and every (re)load resets to it BEFORE the deep call settles | health.js:64-74 | ported | HealthPage.tsx gates the view on query.isFetching (not just the pre-data branch): Refresh and every fresh view entry blank the stale report to the loading placeholders immediately, matching legacy _load resetting summary → "Checking readiness", rail → is-loading strip, findings → "Loading health report" at the top of _load before doctor.status resolves. HealthPage.test.tsx::resets to the loading state on Refresh before the refetch settles; reloads fresh on every view entry instead of serving a cached report. Loading visual polish → manual dev-loop check |
| Success: summary text, status rail class is-<status>, impact count tiles | health.js:80-84,133-150 | ported | HealthPage.test.tsx::HealthPage > calls doctor.status deep… (renders 'Ready with warnings' + summary); HealthPage.tsx StatusRail/CountTile |
| Fallback impactCounts derived from severity counts | health.js:413-420 | ported | logic.test.ts::impactCountsFromSeverity > maps severity counts to impact counts; HealthPage.tsx StatusRail uses impactCounts ?? impactCountsFromSeverity |
| Findings grouped: action/degraded/optional/ready with notes | health.js:277-313 | ported | HealthPage.test.tsx > …renders grouped findings (asserts 'Degraded capabilities' group + note); HealthPage.tsx FindingsSection/GROUPS |
| Impact derivation: readinessImpact else severity mapping | health.js:403-411 | ported | logic.test.ts::impactValue > passes through valid readinessImpact; maps severity error/warn/info/ok |
| Status labels incl. "Ready with warnings" for ready+degraded | health.js:462-472 | ported | logic.test.ts::statusLabel > "Ready with warnings" when ready but degraded; maps action_required |
| Finding card: severity/impact/surface meta, badges (.diagnostic.incomplete, .repair.pending, config.mismatch), restartRequired chip | health.js:324-368 | ported | HealthPage.test.tsx > …renders 'Memory is slow' finding; HealthPage.tsx FindingCard/findingBadge (meta+badges+restart chip); badge/chip variants → manual dev-loop check |
| Evidence tags: max 6, hidden keys restart_required/restartRequired, camelCase->label, JSON values truncated 120 | health.js:439-460,474-483 | ported | logic.test.ts::evidence > hides restart keys and null values; labels camelCase keys keeping each hump capitalized (gatewayUrl -> "Gateway Url", 1:1 with legacy — the earlier `.toLowerCase()` "Gateway url" divergence is removed); labels snake_case keys; truncates long JSON at 120; HealthPage.tsx EvidenceTags slices to 6 |
| Fix steps: numbered, optional command with copy button, heading by kind | health.js:370-401 | ported | HealthPage.test.tsx > …renders 'agentos gateway restart' command; HealthPage.tsx StepsList/CommandRow/stepsHeading |
| Copy command: navigator.clipboard w/ execCommand fallback + ok/err toast | health.js:35-62 + components.js UI.toast | ported (delta) | HealthPage.tsx copyText (clipboard + execCommand fallback) + onCopyCommand: toast.success('Copied command', {id:'health-copy-ok', duration:1600}) / toast.error('Copy failed: …', {id:'health-copy-err', duration:2500}) — matches legacy UI.toast durations (1600ms ok / 2500ms err) and message-dedupe (stable per-outcome id collapses repeats into one toast; legacy keyed by `${type}\0${message}`). HealthPage.test.tsx::copy success fires a 1600ms ok toast with a stable id; copy failure fires a 2500ms err toast with a stable id; re-copying reuses the same ok toast id so identical toasts dedupe. **Residual sonner-seam divergence:** sonner 2.0.7 renders the whole toast list in ONE aria-live="polite" `<section>` and dropped the per-toast `important` option (present in older sonner), so error toasts are NOT announced assertively (role="alert" / aria-live="assertive") the way legacy UI.toast set role="alert" on err/warn toasts — not achievable through the sonner API at this version, revisit if sonner re-adds an assertive path or at cutover. |
| Error state: synthetic gateway.unavailable finding w/ local-vs-remote fix steps, shell-quoted commands | health.js:86-115,191-268 | ported (delta) | HealthPage.test.tsx::HealthPage > renders the synthetic gateway.unavailable finding on RPC failure; shows "Gateway health report unavailable" in the readiness rail, not the raw status token; logic.test.ts::shellArg + gateway url helpers (isLocalGatewayUrl/gatewayStatusTarget); HealthPage.tsx error branch + logic.gatewayUnavailableFixSteps. **Divergence history (now fixed):** an earlier port left the synthetic error report's rail `summary` blank so the readiness rail fell back to the raw "unavailable" status token; restored to legacy behavior (health.js:92-95) — the rail summary now carries the "Gateway health report unavailable" sentence, while the header #health-summary line keeps the distinct "Health report unavailable" (health.js:89). The covering test that had mandated the blank rail summary was updated to assert the sentence. |
| Error-path usesDefault predicate: URL-equality vs the default RPC URL (protocol+host+pathname, query/hash ignored; empty URL falls back true, unparsable/unknown-default false) — NOT mere absence of the localStorage override, since legacy saveConnectionSettings stores the default URL itself (app.js:210) | health.js:227-238 | ported | logic.ts usesDefaultGatewayUrl (bootstrap.ws_url stands in for App.getDefaultRpcUrl()); logic.test.ts::usesDefaultGatewayUrl (5 cases); HealthPage.test.tsx::uses config-target fix steps when the stored wsUrl equals the default; uses gateway-target fix steps when the stored wsUrl differs from the default |
| ^ delta: error-path Config row gated on usesDefault AND isLocalGatewayUrl (legacy gated on usesDefault only) — remote-default deployments no longer show a local configPath in the error evidence/rail; judged more-correct, revisit at cutover if strict parity required | HealthPage.tsx error branch vs health.js:88 | waived (review Task 8) | reviewer finding, owner sign-off at cutover gate; waiver covers ONLY the added isLocalGatewayUrl AND-gate — usesDefault itself follows legacy URL-equality (row above) |
| Refresh button re-runs the report | health.js:17-24 | ported | HealthPage.test.tsx::HealthPage > refetches when Refresh is clicked (click → 2nd doctor.status call); HealthPage.tsx Refresh Button onClick=refetch |
| _gatewayContextUrl() → localStorage['agentos.wsUrl'] \|\| bootstrap.ws_url | health.js:172-185 | ported (simplified) | Legacy read App.loadConnectionSettings().url; new impl reads localStorage['agentos.wsUrl'] ?? bootstrap.ws_url — same effective value. HealthPage.tsx gatewayUrl; exercised via mocked useBootstrap in HealthPage.test.tsx |
| Live parity: /health vs legacy /control/health side-by-side | health.js (whole view) | RTL + manual pending | Live gateway check infeasible: running gateway on :18791 is a stale wheel (2026.7.18.post1, serves index HTML not current JSON contract) and holds the shared ~/.agentos state lock, so a fresh worktree gateway on :18999 refuses to start (pid 8228 owns state_dir); not stopped (user process). Behaviors covered by 12 HealthPage RTL + 22 logic unit tests (grown by this round's health-parity batch — copy toasts, loading-reset, error-rail, usesDefault, evidence casing); visual parity pending a clean gateway |

| Health view visual styling (health-* classes) | css/views/health.css | waived (Plan 1) | behavior-complete, style-pending: HealthPage emits semantic class names with no CSS in new app; styling deferred to shadcn/Tailwind polish pass; final-review finding, owner sign-off at cutover gate |
## Mechanical inventory (generated by scripts/fe_parity_inventory.py)

### RPC methods (57)

| method | legacy sources |
| --- | --- |
| `agents.create` | static/js/views/agents.js, static/js/views/sessions.js |
| `agents.delete` | static/js/views/agents.js |
| `agents.list` | static/js/views/agents.js, static/js/views/sessions.js |
| `agents.update` | static/js/views/agents.js |
| `channels.access.list` | static/js/views/channels.js |
| `channels.access.resolve` | static/js/views/channels.js |
| `channels.access.revoke` | static/js/views/channels.js |
| `channels.access.setMode` | static/js/views/channels.js |
| `channels.status` | static/js/views/channels.js, static/js/views/setup.js |
| `chat.abort` | static/js/views/chat.js |
| `chat.history` | static/js/views/chat.js |
| `chat.send` | static/js/views/chat.js |
| `commands.list_for_surface` | static/js/views/chat.js |
| `config.apply` | static/js/views/config.js |
| `config.get` | static/js/views/approvals.js, static/js/views/chat.js, static/js/views/config.js, static/js/views/setup.js |
| `config.patch` | static/js/views/config.js, static/js/views/setup.js |
| `config.patch.safe` | static/js/views/chat.js |
| `cron.list` | static/js/views/cron.js |
| `cron.remove` | static/js/views/cron.js |
| `cron.run` | static/js/views/cron.js |
| `cron.runs` | static/js/views/cron.js |
| `cron.subscribe` | static/js/views/cron.js |
| `cron.unsubscribe` | static/js/views/cron.js |
| `cron.update` | static/js/views/cron.js |
| `doctor.memory.status` | static/js/views/setup.js |
| `doctor.status` | static/js/views/health.js, static/js/views/overview.js |
| `logs.status` | static/js/views/logs.js |
| `logs.tail` | static/js/views/logs.js |
| `models.list` | static/js/views/chat.js |
| `onboarding.audio.configure` | static/js/views/setup.js |
| `onboarding.catalog` | static/js/views/setup.js |
| `onboarding.channel.probe` | static/js/views/setup.js |
| `onboarding.channel.upsert` | static/js/views/setup.js |
| `onboarding.imageGeneration.configure` | static/js/views/setup.js |
| `onboarding.memory_embedding.configure` | static/js/views/setup.js |
| `onboarding.provider.configure` | static/js/views/setup.js |
| `onboarding.router.configure` | static/js/views/setup.js |
| `onboarding.search.configure` | static/js/views/setup.js |
| `onboarding.status` | static/js/views/setup.js |
| `router.hold.clear` | static/js/views/chat.js |
| `router.hold.set` | static/js/views/chat.js |
| `sessions.contextCompact` | static/js/views/chat.js |
| `sessions.create` | static/js/views/sessions.js |
| `sessions.delete` | static/js/views/sessions.js |
| `sessions.list` | static/js/views/overview.js, static/js/views/sessions.js |
| `sessions.messages.subscribe` | static/js/views/chat.js |
| `sessions.messages.unsubscribe` | static/js/views/chat.js |
| `sessions.reset` | static/js/views/chat.js |
| `skills.deps.install` | static/js/views/skills.js |
| `skills.install` | static/js/views/skills.js |
| `skills.list` | static/js/views/skills.js |
| `skills.search` | static/js/views/skills.js |
| `skills.uninstall` | static/js/views/skills.js |
| `skills.update` | static/js/views/skills.js |
| `status` | static/js/views/overview.js |
| `tools.search_provider` | static/js/views/chat.js |
| `usage.status` | static/js/views/chat.js, static/js/views/overview.js, static/js/views/usage.js |

### Routes (13)

- `/agents`
- `/approvals`
- `/channels`
- `/chat`
- `/config`
- `/cron`
- `/health`
- `/logs`
- `/overview`
- `/sessions`
- `/setup`
- `/skills`
- `/usage`

### Storage keys (13)

| key | legacy sources |
| --- | --- |
| `agent:main:webchat:default` | static/js/views/chat.js |
| `agentos-router-fx` | static/js/views/chat.js |
| `agentos-theme` | static/js/theme.js |
| `agentos-usage-range` | static/js/views/usage.js |
| `agentos-widget:` | static/js/views/chat.js |
| `agentos.chat.debug.enabled` | static/js/views/chat.js |
| `agentos.chat.debugLog` | static/js/views/chat.js |
| `agentos.elevatedMode` | static/js/approval_monitor.js, static/js/views/approvals.js, static/js/views/chat.js |
| `agentos.elevatedMode.version` | static/js/approval_monitor.js, static/js/views/approvals.js, static/js/views/chat.js |
| `agentos.savingsFx` | static/js/components/savings-fx.js |
| `agentos.wsToken` | static/js/app.js |
| `agentos.wsUrl` | static/js/app.js |
| `agentos_active_session` | static/js/views/chat.js, static/js/views/cron.js |
