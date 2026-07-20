# Console Rewrite Parity Matrix

Single source of truth for migration completeness (spec §6). A behavior row
may be `pending`, `ported` (with evidence: test name or verification note),
or `waived` (with reason, owner-approved at cutover).

Row format:
| behavior | legacy source | status | evidence / reason |

## Cross-cutting
| behavior | legacy source | status | evidence / reason |
| --- | --- | --- | --- |
| Theme persistence + system-default resolution | js/theme.js:8-38 | ported | theme.test.ts::theme store > initTheme applies stored preference; set persists and applies; toggle flips the mode; rejects invalid modes |
| Theme flash prevention inline script | templates/index.html (head) | pending | |
| WS handshake: connect.challenge -> connect(protocol 3) -> HelloOk+policy | js/rpc.js:87-127 | ported | ws-rpc.test.ts::handshake > answers connect.challenge with a protocol-3 connect request incl. auth token; enters connected state and stores policy on HelloOk |
| WS req/res correlation + typed errors (code/details) | js/rpc.js:45-147 | ported | ws-rpc.test.ts::call correlation > resolves with payload on ok res, matching by id; rejects with RpcError carrying code and details; rejects immediately when not connected; rejects all pending calls when the socket closes |
| WS event fan-out incl. wildcard '*' listener | js/rpc.js:148-154 | ported | ws-rpc.test.ts::events > fans out to named and wildcard listeners with meta |
| WS seq-gap detection -> close+reconnect (_gap) | js/rpc.js:188-202 | ported | ws-rpc.test.ts::events > detects a seq gap, emits _gap, and closes the socket |
| WS tick-watch (policy.tick_interval_ms, 2.5x timeout) | js/rpc.js:204-217 | ported | ws-rpc.test.ts::keepalive and reconnect > closes the socket when no frame arrives within the tick timeout |
| WS keepalive ping every 55s | js/rpc.js:172-179 | ported | ws-rpc.test.ts::keepalive and reconnect > sends a ping every 55s while open |
| WS reconnect backoff 800ms x1.7 max 15s | js/rpc.js:226-231 | ported | ws-rpc.test.ts::keepalive and reconnect > reconnects with backoff after close (800ms first retry) |
| Default route: /overview desktop, /chat on <=768px | js/router.js:32 | ported | app/routes.tsx::defaultPath (index route Navigate); matchMedia('(max-width: 768px)') → /chat else /overview |
| 404 route fallback rendered as text (XSS-safe) | js/router.js:48-55 | ported | AppShell.test.tsx::routes > renders XSS-safe 404 text for unknown paths (asserts no <script> injected); app/routes.tsx::NotFound renders path as JSX text |
| Document title per route ("<Title> - AgentOS Control") | js/router.js:68-71 | ported | AppShell.test.tsx::routes > sets the document title from the route; views/StubView.tsx sets document.title in useEffect |
| Nav active state + aria-current | js/router.js:59-66 | ported | app/AppShell.tsx NavLink (React Router sets aria-current="page" + active className on match); manual dev-loop check |
| Bootstrap data: version/ws_url/auth_mode/base_path/config_path/features | control_ui.py:_build_bootstrap_context | ported | test_control_ui_bootstrap.py::test_bootstrap_returns_json_context |
| Bootstrap consumption: fetch /api/bootstrap, connect WS (stored wsUrl/wsToken override), mirror _state into connection store | js/app.js (bootstrap fetch + ws connect) | ported | lib/bootstrap.ts + app/providers.tsx; covered indirectly by Task 7/8 tests |
| Stored WS override wins over bootstrap ws_url (agentos.wsUrl / agentos.wsToken) | js/app.js | ported | app/providers.tsx (WS_URL_KEY/WS_TOKEN_KEY); covered indirectly by Task 7/8 tests |
| Connection banner: show while connecting/disconnected, clear on connect | js/app.js (connection status UI) | ported | app/AppShell.tsx (connState !== 'connected' → role=status banner; text per connecting/disconnected); manual dev-loop check |
| noscript message | templates/index.html | pending | |
| Feature flag AGENTOS_FEATURES.tokenViz (default false) | js/app.js:6-9 | pending | |
| Custom base_path support for built assets | control_ui.py + vite base | pending | cutover-plan item |

## Views
(One section per view; filled by each view's Task before implementation.
 Health is filled in this plan; the other 12 in later plans.)

### health
| behavior | legacy source | status | evidence / reason |
| --- | --- | --- | --- |
| doctor.status RPC {agentId:'main', deep:true} after waitForConnection | health.js:76-77 | ported | HealthPage.test.tsx::HealthPage > calls doctor.status deep for agent main and renders grouped findings (asserts call('doctor.status', {agentId:'main', deep:true}) after waitForConnection) |
| Loading state: "Checking readiness" + loading strip | health.js:64-74 | ported | HealthPage.tsx LoadingRail + summaryText 'Checking readiness' (pre-data branch); RTL covers success/error; loading visual → manual dev-loop check |
| Success: summary text, status rail class is-<status>, impact count tiles | health.js:80-84,133-150 | ported | HealthPage.test.tsx::HealthPage > calls doctor.status deep… (renders 'Ready with warnings' + summary); HealthPage.tsx StatusRail/CountTile |
| Fallback impactCounts derived from severity counts | health.js:413-420 | ported | logic.test.ts::impactCountsFromSeverity > maps severity counts to impact counts; HealthPage.tsx StatusRail uses impactCounts ?? impactCountsFromSeverity |
| Findings grouped: action/degraded/optional/ready with notes | health.js:277-313 | ported | HealthPage.test.tsx > …renders grouped findings (asserts 'Degraded capabilities' group + note); HealthPage.tsx FindingsSection/GROUPS |
| Impact derivation: readinessImpact else severity mapping | health.js:403-411 | ported | logic.test.ts::impactValue > passes through valid readinessImpact; maps severity error/warn/info/ok |
| Status labels incl. "Ready with warnings" for ready+degraded | health.js:462-472 | ported | logic.test.ts::statusLabel > "Ready with warnings" when ready but degraded; maps action_required |
| Finding card: severity/impact/surface meta, badges (.diagnostic.incomplete, .repair.pending, config.mismatch), restartRequired chip | health.js:324-368 | ported | HealthPage.test.tsx > …renders 'Memory is slow' finding; HealthPage.tsx FindingCard/findingBadge (meta+badges+restart chip); badge/chip variants → manual dev-loop check |
| Evidence tags: max 6, hidden keys restart_required/restartRequired, camelCase->label, JSON values truncated 120 | health.js:439-460,474-483 | ported | logic.test.ts::evidence > hides restart keys and null values; labels camelCase keys; truncates long JSON at 120; HealthPage.tsx EvidenceTags slices to 6 |
| Fix steps: numbered, optional command with copy button, heading by kind | health.js:370-401 | ported | HealthPage.test.tsx > …renders 'agentos gateway restart' command; HealthPage.tsx StepsList/CommandRow/stepsHeading |
| Copy command: navigator.clipboard w/ execCommand fallback + ok/err toast | health.js:35-62 | ported | HealthPage.tsx copyText (clipboard + execCommand fallback) + onCopyCommand (toast.success/error); clipboard interaction → manual dev-loop check |
| Error state: synthetic gateway.unavailable finding w/ local-vs-remote fix steps, shell-quoted commands | health.js:86-115,191-268 | ported | HealthPage.test.tsx::HealthPage > renders the synthetic gateway.unavailable finding on RPC failure; logic.test.ts::shellArg + gateway url helpers (isLocalGatewayUrl/gatewayStatusTarget); HealthPage.tsx error branch + logic.gatewayUnavailableFixSteps |
| Refresh button re-runs the report | health.js:17-24 | ported | HealthPage.test.tsx::HealthPage > refetches when Refresh is clicked (click → 2nd doctor.status call); HealthPage.tsx Refresh Button onClick=refetch |
| _gatewayContextUrl() → localStorage['agentos.wsUrl'] \|\| bootstrap.ws_url | health.js:172-185 | ported (simplified) | Legacy read App.loadConnectionSettings().url; new impl reads localStorage['agentos.wsUrl'] ?? bootstrap.ws_url — same effective value. HealthPage.tsx gatewayUrl; exercised via mocked useBootstrap in HealthPage.test.tsx |
| Live parity: /health vs legacy /control/health side-by-side | health.js (whole view) | RTL + manual pending | Live gateway check infeasible: running gateway on :18791 is a stale wheel (2026.7.18.post1, serves index HTML not current JSON contract) and holds the shared ~/.agentos state lock, so a fresh worktree gateway on :18999 refuses to start (pid 8228 owns state_dir); not stopped (user process). Behaviors covered by 5 RTL + 16 logic unit tests; visual parity pending a clean gateway |

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
