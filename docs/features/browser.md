# Browser automation

The `browser` tool lets the agent drive a real Chromium — navigate, read a page
as an accessibility snapshot with element refs, click, type, fill forms, wait,
run JavaScript, answer native dialogs, and take screenshots. It is built on the
[`agent-browser`](https://github.com/vercel-labs/agent-browser) CLI engine
(Vercel Labs, Apache-2.0), a Rust client–daemon that speaks the Chrome DevTools
Protocol directly.

The tool stays **hidden** from the model until the `agent-browser` binary is
installed, the same way `x_search` hides without an xAI key.

## Install

```
npm install -g agent-browser
agent-browser install          # downloads Chromium
# on Debian/Ubuntu/Docker, also install system libs:
agent-browser install --with-deps
```

`agentos doctor` reports whether the binary and Chromium are present and prints
this hint when they are missing.

## Modes

**Managed (default).** The engine launches its own headless Chromium. Nothing to
configure beyond installing the binary. Set `headless = false` to watch it work
in a visible window.

**Attach (opt-in, local only).** Point the tool at your own already-running
Chrome so the agent can act inside your logged-in sessions. Start Chrome with a
debug port bound to loopback:

```
# macOS example
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222
```

then set:

```toml
[browser]
cdp_port = 9222
attach_confirmed = true   # required — see below
```

Only an integer **port** is accepted (localhost only); there is no way to point
the tool at a remote or cloud CDP URL. This is deliberate — it structurally
rules out the agent (or a page it visits) redirecting the browser backend
somewhere else.

### Attach consent

Attach mode can drive whatever your Chrome is signed into — email, bank,
dashboards. Because that is a real risk, attach mode refuses to run until you
explicitly consent by setting `attach_confirmed = true`. Setting the port alone
is not enough. Managed headless mode needs no confirmation.

## Actions

`browser(action, …)` dispatches on `action`:

| action | args | what it does |
| --- | --- | --- |
| `navigate` | `url` | open a URL; returns final URL, title, and a page snapshot |
| `snapshot` | — | accessibility tree with `@e` refs (the default "read the page") |
| `click` | `ref` | click an element by ref |
| `type` | `ref`, `text` | type into a field (secret-guarded) |
| `fill` | `ref`, `text` | clear then set a field's value (secret-guarded) |
| `select` | `ref`, `value` | choose a `<select>` option |
| `wait` | `condition` | wait for a selector / text / URL / load state |
| `press` | `key` | keyboard key (Enter, Tab, …) |
| `scroll` | `direction` | scroll the viewport |
| `back` | — | history back |
| `screenshot` | — | capture the page |
| `tabs` | — | list tabs in this session |
| `eval` | `expression` | run JavaScript in the page |
| `dialog` | `dialog_action` | accept/dismiss a native alert/confirm/prompt |
| `close` | — | end this session's browser (managed) / disconnect (attach) |

`navigate`, `snapshot`, `click`, `type`, `fill`, and `wait` are the everyday
loop. `eval` and `dialog` are covered below.

## How the agent decides to use it

You should not have to say "use the browser tool". The tool's description tells
the model to reach for it whenever you ask to open, browse, or look at a page,
name a site or a search engine ("search Google for…", "check it on x.com"), want
to watch the browser work, or need clicking, a form, or a signed-in session. An
explicit request for a browser or a named site wins over `web_search` — that
substitution is the main way this tool ends up looking absent.

The description also carries a runtime line naming which browser the session
drives, because that changes when the tool is the right choice:

- **Attached** (visible, your own browser): search engines that refuse headless
  automation answer normally, so the model is told to prefer this tool over
  `web_search` for search and browsing.
- **Headless**: Google and DuckDuckGo answer with a CAPTCHA, so the model is
  told to use `web_search` for general search and keep this tool for pages that
  need interaction or that allow automated access.

## Sessions and persistence

Each AgentOS session gets one browser session, started lazily on the first
`browser` call and reused across turns. Idle sessions are reaped after
`session_ttl_minutes`; at most `max_sessions` run at once (oldest-idle evicted).

By default a session's profile is **ephemeral** — cookies and logins do not
survive it. Set `persist_profile = true` to keep them between sessions; note that
this writes cookies and storage to disk under the AgentOS state directory.

## Security posture

The engine runs **outside** the sandbox — Chromium cannot run inside
bubblewrap/seatbelt — so these controls are enforced in AgentOS instead:

- **Environment scrub.** The engine subprocess starts from a minimal environment
  (PATH, HOME, locale, …), never the full environment. The gateway token and all
  provider API keys are unreachable from the browser process. (Lesson from a real
  Hermes incident, GHSA-m4m8-xjp4-5rmm.)
- **SSRF.** `navigate` refuses private ranges, loopback, and cloud-metadata hosts
  (`169.254.169.254`, …), and re-checks the final URL after redirects. `about:`
  targets (like `about:blank`) are allowed; `file://` and `data:` URLs are refused.
- **Private-page read guard.** In managed mode, a read action re-checks the live
  page URL — a JavaScript redirect onto a private address after navigation does
  not let the next snapshot exfiltrate it. Relaxed in attach mode (your Chrome
  legitimately sits on intranet pages, and the attach consent covers that).
- **Untrusted envelope.** Everything the engine returns — snapshots, `eval`
  results, tab titles and URLs, every action payload — is wrapped in the
  `<untrusted>` envelope, so page content can't smuggle instructions to the
  agent. `eval("document.body.innerText")` is the shortest path from a page into
  the transcript, so it carries the boundary like any other read.
- **Output redaction.** Snapshots, console output, and eval results are masked
  for credentials before reaching the model.
- **Secret-typing guard.** `type` and `fill` refuse text that looks like a
  credential or matches a value in the environment store.
- **No cron.** The tool is excluded from the cron read-only surface.
- **Domain allowlist (optional).** `allowed_domains = []` (the default) allows
  the open web. When you set a list, navigation off it is refused, and the list
  is also passed to the engine (`--allowed-domains`) so in-page JavaScript
  navigation is bounded too.

## eval and JavaScript

`eval` runs a JavaScript expression in the page and returns its value. It is
powerful: JavaScript can read `document.cookie`, storage, and form values — so
`eval` can reach things the secret-typing guard blocks for `type`/`fill`. That
tradeoff is intentional (it matches how Hermes exposes eval), mitigated by
output redaction and an **opt-in** denylist:

```toml
[browser]
restrict_evaluate = true    # block document.cookie, fetch, storage, … in eval
allow_unsafe_evaluate = false
```

`restrict_evaluate` is **off by default** because gating on primitive names
cripples ordinary DOM extraction. When on, it refuses sensitive primitives
(including obfuscated spellings like `document["coo"+"kie"]`); set
`allow_unsafe_evaluate = true` to override for a trusted page.

`eval` is SSRF-pre-scanned in **both** modes: an expression containing an
`http(s)://` literal that points at a private, loopback, or cloud-metadata
address is refused before it runs. The managed browser is not exempt — it runs
on your machine and reaches those addresses exactly as your own browser does. In
managed mode the page URL is also re-checked after the call, so an expression
that navigates somewhere private cannot return that page's content.

`file` upload and raw CDP passthrough are intentionally not exposed.

## Dialogs

Native `alert` / `confirm` / `prompt` / `beforeunload` dialogs are captured by a
CDP supervisor and surfaced as `pending_dialogs` in a snapshot. Respond with
`browser(action="dialog", dialog_action="accept" | "dismiss", prompt_text=…)`.
Policy for unanswered dialogs:

```toml
[browser]
dialog_policy = "must_respond"   # or auto_dismiss | auto_accept
dialog_timeout_s = 300
```

Dialog interception needs a CDP endpoint: it works in attach mode, and in
managed mode when the engine exposes its (loopback) CDP endpoint. If no endpoint
is available the browser still works; dialogs just aren't intercepted.

## Configuration reference

```toml
[browser]
enabled = true                # master switch; false hides the tool even if installed
headless = true               # managed mode; false opens a visible window
binary_path = ""              # optional explicit path to agent-browser
cdp_port = 0                  # 0 = managed. >0 = attach to your Chrome (localhost only)
attach_confirmed = false      # must be true to let the agent drive your Chrome
allowed_domains = []          # [] = open web (SSRF still blocks private ranges)
persist_profile = false       # true = keep cookies/login between sessions (on disk)
session_ttl_minutes = 15
max_sessions = 3
snapshot_max_chars = 24000    # snapshots over this are truncated with a marker
dialog_policy = "must_respond"  # must_respond | auto_dismiss | auto_accept
dialog_timeout_s = 300.0
restrict_evaluate = false     # true = block sensitive JS primitives in eval
allow_unsafe_evaluate = false # true = override restrict_evaluate for trusted pages
```

Every field can also be set via `AGENTOS_BROWSER_*` environment variables.

## Limitations

- **Anti-bot walls.** Managed headless Chromium is detectable, and major search
  engines block it. Verified on 2026-08-21: `google.com/search` returns a
  reCAPTCHA ("Our systems have detected unusual traffic from your computer
  network"), `duckduckgo.com` returns an image challenge, and `booking.com`
  answered a bare `502`. Sites that did work headless in the same test:
  `bing.com/search` and Wikipedia. The agent must never solve a CAPTCHA — when
  it hits one, the honest move is to report the wall and use a source that
  allows automated access, or `web_search` / `web_fetch` instead. For sites that
  need a real signed-in identity, attach mode (your own Chrome) is the intended
  path, not fingerprint evasion.
- Cloud CDP providers (Browserbase, Browser Use) are not supported.
- File upload/download, raw CDP passthrough, and vision-model screenshot analysis
  are out of scope.
- In-page sub-resource requests are not intercepted for SSRF in managed mode
  (only top-level navigation is).
