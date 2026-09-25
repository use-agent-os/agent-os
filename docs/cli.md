# CLI Reference

The `agentos` CLI is the fastest way to configure, run, inspect, and
automate AgentOS.

Run:

```sh
agentos --help
agentos <command> --help
agentos --version
```

`--version` prints the installed version and exits, so the version is
available without `uv tool list` or `pip show`.

## Main Commands

| Command | Purpose |
| --- | --- |
| `agentos init` | Initialize a workspace. |
| `agentos upgrade` | Upgrade AgentOS and restart the managed gateway to match. |
| `agentos doctor` | Diagnose readiness and print recovery steps. |
| `agentos onboard` | Run or inspect first-run setup. |
| `agentos auth` | Provider logins that are not API keys (`login`/`status`/`logout`; xAI today). |
| `agentos configure` | Reconfigure provider, router, channels, search, x-search, image generation, or memory embedding. |
| `agentos gateway` | Run and manage the gateway server. |
| `agentos chat` | Start interactive terminal chat. |
| `agentos agent` | Run a single automation-friendly agent turn. |
| `agentos sessions` | List, inspect, rename, resume, abort, delete, or export sessions. |
| `agentos projects` | Group sessions into projects with shared knowledge injected into every member session. |
| `agentos wallet` | Create, import, export and unlock wallets in the engine's vault; show balances. |
| `agentos trade` | Quote and swap tokens on Base / Robinhood Chain through the AgentOS Aggregator (default) or Uniswap; orders, approvals, history, PnL. |
| `agentos skills` | List, search, view, install, update, publish, inspect, and tap skills. |
| `agentos memory` | Inspect and maintain memory. |
| `agentos channels` | Configure and inspect messaging channels. |
| `agentos providers` | Configure and inspect LLM providers. |
| `agentos search` | Configure and use web search. |
| `agentos sandbox` | Inspect or change default sandbox posture. |
| `agentos cron` | Manage scheduled AgentOS runs. |
| `agentos cost` | Inspect usage and estimated cost. |
| `agentos cost savings` | Report what the Pilot Router saved against the priciest configured tier. |
| `agentos context` | Show the fixed per-request context cost and what each tool profile would cost. |
| `agentos diagnostics` | Enable or disable runtime diagnostics logging. |
| `agentos replay` | Replay a recorded turn from the decision log. |
| `agentos migrate` | Import state from external agent runtimes. |
| `agentos models` | Inspect available models. |
| `agentos agents` | Manage durable agents. |
| `agentos mcp-server` | Run the AgentOS MCP server bridge. |
| `agentos dist` | Emit a reproducible workspace-state inventory. |
| `agentos reset` | Reset a session, rotating it to a fresh transcript. |

## Run Surfaces

Web UI and gateway:

```sh
agentos gateway run
agentos gateway start --json
agentos gateway status
agentos gateway restart
agentos gateway stop
```

`agentos gateway status` (and `--json`) reports **both** the installed CLI
version (`cliVersion`) and the running gateway's version (`gatewayVersion`);
when they differ it sets `versionMismatch` and prints a diagnostic advising a
restart — the normal state right after a package upgrade with `--no-restart`,
or after a manual upgrade.

Terminal chat:

```sh
agentos chat
agentos chat --model gpt-5.4-mini
agentos chat --session <session-key>
agentos chat --standalone --workspace /path/to/project
```

### Chat REPL slash commands

`agentos chat` exposes a prompt-toolkit REPL with a slash-command palette.
The most useful ones:

| Command | Purpose |
| --- | --- |
| `/new [title]` | Start a new chat session. The optional title is persisted as the session's display name and shown in the bottom toolbar and `/status`. |
| `/resume <key>` | Resume an existing session by key (or a prefix / display-name match in gateway mode). |
| `/status` | Show the current session, model, permissions, and the active Pilot Router tier (or `auto`). |
| `/model <id>` | Override the model for this session. |
| `/clear` / `/reset` | Clear the current conversation context. The screen is wiped too (including scrollback), so the cleared turns are gone from view as well as from context. |
| `/compact` | Compact older context into a summary. |
| `/cost` | Show per-session token and cost totals. |
| `/save [path]` | Save the transcript to a Markdown file. |
| `/c0` … `/c3` | Pin the Pilot Router to a configured tier for this session. The pin appears in the bottom toolbar (e.g. `tier:c3`) and stays active until you exit or run `/auto`. |
| `/use <model-id>` | Pin the route to a specific model, outside the configured tiers. The model must belong to the active provider. |
| `/auto` | Restore automatic Pilot Router routing (clears the tier pin). |
| `/plan [off]` | Toggle plan mode: the session becomes research-only (read, search, and analysis tools) until you approve the plan the agent presents via `exit_plan_mode`. `/plan off` leaves plan mode — from text surfaces it is also how you approve a presented plan before telling the agent to proceed. Gateway mode only. |
| `/help` | List the commands available on the current surface. |
| `/exit` / `/quit` | Leave the REPL. |

Router tier commands (`/c0` … `/c3`, `/auto`) are available in both gateway
and `--standalone` modes. Tiers not present in your `[agentos_router]`
config are rejected with a readable error. In `--standalone` mode the
router must be enabled in config; otherwise the command reports
"Pilot Router is disabled or unavailable."

A tier pin you set is **sticky**: it holds every turn until you clear it with
`/auto` (or the process ends). It does not time out. Pin `/c3` and forget, and
every later turn keeps paying for `c3` — check `/status` if you are unsure what
is in force. This differs from the routing the model may pick for itself
mid-turn, which still lapses on its own after ten idle minutes.

`/use` pins a model that is not one of your four configured tiers. It is a
separate verb from `/model`, whose argument filters the model listing — `/model
gpt` shows you the gpt models, `/use gpt-5.6-terra` switches to one. Because
every turn runs through the single configured `llm.provider` (a tier's
`provider` field is metadata, not a client selector), only that provider's
models can be pinned; anything else is refused at the point of choosing rather
than failing on the next turn. A directly-named model rides on the default tier,
inheriting its thinking level and pricing baseline — settings a bare model id
does not carry.

While a pin is in force the model's own `router_control` tool is withdrawn, so
it cannot route around your choice. Two exceptions are worth knowing:

- **Image turns.** A turn with an image attachment is routed to a vision-capable
  tier before pins are consulted, so it runs on that tier, not your pinned one.
  The Web UI flags such a turn.
- **Large context.** A pinned turn skips the large-context tier floor. If the
  conversation outgrows the pinned model's context window, the turn fails at the
  provider rather than being quietly upgraded. Pin a larger tier, or run `/auto`.

### Assistant label and session chrome

The assistant speaker label shown on the `◢` marker and the pre-token
waiting row defaults to `agentos`. Override it with the
`AGENTOS_ASSISTANT_LABEL` environment variable — the value is read once at
startup and used by every renderer, so it stays consistent across the
streamed reply marker, the waiting header, and the queued-turn marker.

```sh
AGENTOS_ASSISTANT_LABEL="Hani" agentos chat
```

The active input row is framed by a top and bottom rule, so the typing
area reads as a distinct box between the transcript and the bottom
toolbar:

```
────────────────────────────────────────
 ◢ you  <your message here>
────────────────────────────────────────
 title · model · [tier:cN]
```

Press `Enter` to submit the current message. Use `Alt+Enter` or
`Shift+Enter` to insert a newline when your terminal reports those modified
keys distinctly; `Ctrl+J` is the portable newline fallback. The input frame
grows with the message up to 10 visible lines, then scrolls internally while
remaining pinned above the bottom toolbar. `Up` and `Down` move between lines
in a multiline draft before moving through chat input history at the first or
last line.

The bottom toolbar renders `title · model · [tier:cN]` while typing. The
title comes from `/new <title>` (or is loaded from the gateway on
`/resume`); the tier chip appears only while a Pilot Router hold is
active. `/status` mirrors the same fields plus the active permissions
posture.

**Full-screen surface (default).** `agentos chat` renders the conversation
in a scrollable in-app pane above a permanently-pinned input frame (Claude
Code style), so the frame stays visible while the assistant streams. The
branded welcome screen renders at the top of the pane on launch. `PgUp`/`PgDn`
scroll back through history; the mouse wheel scrolls when the pointer is over
the transcript. New output re-pins to the newest line.

**Select and copy.** Drag with the left mouse button across the transcript
to highlight any span (the selection shows in reverse video); releasing the
button copies the plain text — ANSI styling stripped, CJK width-aware — to
the system clipboard (`pbcopy` on macOS, `wl-copy`/`xclip`/`xsel` on Linux,
`clip` on Windows, OSC 52 escape as a fallback). Click anywhere to clear the
selection. The emulator's own selection gesture (typically `Shift+drag` on
Linux, `Option+drag` in iTerm2) also still works if you prefer it.

**Markdown rendering.** The assistant's streamed reply is styled inline as
it arrives: `#`/`##`/`###` headings render in the brand accent, `>`
quotes get a dimmed bar, `---` becomes a rule, list markers are tinted,
tables keep their pipes aligned, fenced code blocks stream in a uniform
code color (no waiting for the closing fence), and inline spans —
`**bold**`, `*italic*`, `~~strike~~`, inline `` `code` ``, and
`[text](url)` links —
are styled in place. File names, branch names, and other important terms
the model wraps in backticks stand out in the accent color. Reasoning-model
`<think>…</think>` blocks render as a recessive gray-bar, dim-italic region
(the tags themselves are hidden) so the chain-of-thought stays visible but
never competes with the reply. The render is
write-once (no repaint loop), and `NO_COLOR` (or a non-color terminal)
downgrades the stream to plain text so piped output stays greppable.

Input navigation follows the current logical line in multiline drafts:
`Home`/`End` and `Ctrl+A`/`Ctrl+E` move to that line's start/end. On macOS,
`Cmd+Left`/`Cmd+Right` work when the terminal maps those shortcuts to
`Home`/`End`; use `Ctrl+A`/`Ctrl+E` as the portable fallback.

Full-screen is the default for an interactive terminal. Non-TTY / piped
invocations fall back to native scrollback automatically. To force a mode set
`AGENTOS_CHAT_FULLSCREEN`:

```sh
AGENTOS_CHAT_FULLSCREEN=0 agentos chat   # opt out — stream to native scrollback
AGENTOS_CHAT_FULLSCREEN=1 agentos chat   # force full-screen (e.g. under a pipe)
```

One-shot automation:

```sh
agentos agent -m "Review the current directory"
agentos agent --json -m "Return a short machine-readable summary"
agentos agent --workspace /path/to/project --workspace-strict -m "Inspect this repo"
agentos agent --timeout 600 --max-iterations 30 -m "Run a bounded investigation"
```

Useful automation flags:

| Flag | Purpose |
| --- | --- |
| `--workspace` | Set the workspace root. |
| `--workspace-strict` | Restrict read-side file tools to the workspace. |
| `--workspace-lockdown` | Contain writes to workspace or scratch directory. |
| `--scratch-dir` | Place temporary scripts/logs/candidate patches in a known directory. |
| `--file` / `-f` | Attach a local file; repeat for multiple files. |
| `--unattended` / `--interactive` | Run without a live approval surface (unattended is default). |
| `--stateless` / `--clean-room` | Use clean-room prompt bootstrap. |
| `--stateless-keep-project-rules` | With clean-room bootstrap, keep `AGENTS.md` project rules only. |
| `--no-memory-capture` | Do not write this invocation to durable searchable memory. |
| `--session-id` | Target a specific session key/id for cross-invocation continuity. |
| `--timeout` / `-T` | Set total agent wall-clock timeout in seconds. |
| `--max-iterations` | Bound the model/tool loop. |
| `--iteration-timeout-seconds` | Per-iteration timeout in seconds (one LLM call + tool executions). |
| `--tool-timeout-seconds` | Per-tool execution timeout in seconds. |
| `--request-timeout-seconds` | Single LLM HTTP/streaming request timeout in seconds. |
| `--max-provider-retries` | Bound transient provider retries. |
| `--length-capped-continuations` | Bound automatic continuations after length-limited provider output. |
| `--thinking` | Override reasoning level (off, minimal, low, medium, high, xhigh, adaptive). |
| `--permissions` | Select restricted, bypass, or full permission posture. |
| `--transcript-path` | Write a JSONL transcript for automation. |
| `--usage-path` | Write usage JSON. |
| `--session-db-path` | Persist session replay across invocations. |
| `--json` | Emit machine-readable JSON output. |

## Version

```sh
agentos --version     # or -V: prints the installed version, nothing else
```

Deliberately cheap (no config load, no gateway probe): the macOS app runs it
at every launch to decide whether the engine it ships needs installing.

## Installer stage protocol

`install.sh` is also the engine installer behind the macOS app, which drives it
stage by stage so it can show real progress and retry one step:

```sh
bash install.sh --manifest            # one JSON line: {"protocol_version":1,"stages":[…]}
bash install.sh --stage uv --json     # run ONE stage; the last stdout line is the result frame
                                      #   {"ok":true|false,"stage":"uv","skipped":bool[,"reason":"…"]}
```

Stages, in order: `prerequisites` (platform, curl/wget, reachability of
github.com and astral.sh), `uv` (download-then-run the astral installer if uv is
missing), `python` (`uv python install 3.12` if needed), `package`
(`uv tool install --force` the version-pinned wheel, then smoke-test the entry
point), `path` (append the uv tool bin dir to the login shell's rc file once),
`complete`. Every `--stage` call is a separate process, each stage body runs in
a subshell so a helper's `exit 1` still yields `{"ok":false}`, and
`--non-interactive` makes a stage that would need input report `skipped:true`
(none do today). A plain `bash install.sh` runs all stages in order as before.

## Upgrade

`agentos upgrade` is the primary upgrade path. It detects how AgentOS was
installed, installs the **published PyPI release** of
`use-agent-os[recommended]`, and — by default — restarts the managed gateway and
**verifies** the running gateway reports the new version before declaring
success (a "successful" upgrade that leaves the daemon on old code is the
common upgrade regret).

It always targets the release, never a local checkout. To install a checkout,
run `bash scripts/install_source.sh` — that script is the only path that
rebuilds the React control UI (`npm ci && npm run build`) before installing.

```sh
agentos upgrade                 # snapshot, upgrade, restart the gateway, verify
agentos upgrade --check         # is a newer release available? change nothing
agentos upgrade --dry-run       # print the exact command that would run
agentos upgrade --no-restart    # upgrade only; leave the gateway on OLD code
agentos upgrade --source github # install the GitHub release asset, not PyPI
agentos upgrade --timeout 900   # bound the upgrade subprocess (default 600s)
agentos upgrade --verify-data   # only check the state databases
agentos upgrade --restore-snapshot latest   # put the last snapshot back
```

| Flag | Purpose |
| --- | --- |
| `--check` | Ask PyPI and GitHub Releases for a newer release (5s timeout each); offline prints `could not check (offline)`. Changes nothing. `--json` adds `pypi`, `github` and the `source` an upgrade would use. |
| `--dry-run` | Print the upgrade command that would run, whether a snapshot would be taken and whether the gateway would be restarted; touch nothing. |
| `--source` | `auto` (default): PyPI, or the GitHub release wheel when GitHub is ahead or PyPI is unreachable. `pypi` / `github` force one. |
| `--no-snapshot` | Skip the pre-upgrade snapshot (default: taken). |
| `--no-restart` | Upgrade the package but do not restart the gateway. Prints an unmissable warning that it still runs the old version; run `agentos gateway restart` yourself. The data check is skipped too — only a restarted gateway has migrated anything. |
| `--timeout` | Upgrade-subprocess timeout in seconds (default 600). On timeout the tool's process group is killed with recovery guidance — never a half-state. |
| `--verify-data` | Run `PRAGMA quick_check` on every SQLite file under `~/.agentos/state/` and exit; nothing else happens. Exit 1 on a problem, naming the last snapshot. |
| `--restore-snapshot DIR\|latest` | Copy a pre-upgrade snapshot back file for file. Refuses while a gateway answers on the configured endpoint (a live database would replay its journal over the restored file). |
| `--config` | Target a specific config file for the gateway restart. |
| `--json` | Machine-readable output. |

### Release sources

Every release is published twice — a wheel on PyPI and the same wheel attached
to the GitHub release the tag created (`install.sh` installs from the latter).
`--source auto` prefers PyPI and falls back to the GitHub asset only when GitHub
is *ahead* (the "PyPI publish failed for this tag" case) or PyPI cannot be
reached; the spec then becomes
`use-agent-os[recommended] @ https://github.com/use-agent-os/agent-os/releases/download/v<version>/use_agent_os-<version>-py3-none-any.whl`,
which `uv tool install` / `pipx install` accept as-is. `AGENTOS_REPOSITORY`
(`owner/name`) points both `install.sh` and the upgrade at a fork.

### Snapshot and data check

Before the installer runs, `config.toml`, `auth.json`, `skills-lock.json` and
every `*.db` / `*.sqlite` under `~/.agentos/state/` are copied into
`~/.agentos/state/snapshots/pre-upgrade-<utc>/` (databases through SQLite's
online-backup API, so a WAL-mode file the gateway is writing still yields a
consistent copy; files over 1 GiB are skipped and listed; `.env` is never
copied). The newest three snapshots are kept.

After the restarted gateway has verifiably reported the new version — and so
has run its config and schema migrations — every state database gets a
`PRAGMA quick_check`. If one fails and there is a snapshot, the managed gateway
is stopped, the snapshot restored, the gateway started again and the check
repeated; the JSON output carries `data` and `restored`. A gateway this command
does not manage is left alone and the exact `--restore-snapshot` command is
printed instead.

### From the Control UI and the desktop app

The web console's update banner has an **Update now** button: it calls the
`updates.apply` RPC, which spawns `agentos upgrade --json` as a detached job
(it survives the gateway restart it causes) and records progress under
`~/.agentos/state/upgrade_job.{json,log}`; `updates.status` reports
`idle | running | done | failed` plus the log tail and the final JSON, from
whichever gateway process is up. `updates.verifyData` runs the data check on
demand. The macOS app runs `agentos upgrade --json --no-restart` itself,
restarts the gateway it spawned, then confirms the version and data over RPC
(Settings › About).

Per install method:

- **uv tool** — delegated automatically as
  `uv tool install --force --python <running major.minor> "use-agent-os[recommended]"`,
  resolving `uv` to an absolute path over a hardened PATH. `install` rather than `upgrade` is
  load-bearing: `uv tool upgrade` takes only a bare tool name and re-resolves
  whatever uv's receipt recorded, so an install laid down from a checkout
  (`install_source.sh` passes `.`) keeps rebuilding the wheel from the working
  tree — re-packaging whatever `src/agentos/gateway/static/dist/` is on disk,
  because nothing in the upgrade path runs `npm run build`. `--force` is
  required so an already-installed tool is genuinely rebuilt instead of
  no-op'ing, and it also self-heals a stale cache or an orphaned interpreter
  (e.g. after the base Python moves). `--python` pins the rebuilt venv to the
  interpreter already in use, so a forced reinstall never moves a 3.13 install
  onto another version.
- **pipx** — the same shape: `pipx install --force "use-agent-os[recommended]"`.
- **pip / editable / unknown** — not faked: prints the exact manual command
  (e.g. `python -m pip install --upgrade "use-agent-os[recommended]"`) and exits
  with a distinct code. The editable hint points at
  `git pull && bash scripts/install_source.sh`, since an editable install serves
  the control UI straight out of the checkout.

Extras are always `[recommended]` — the same profile `install_source.sh`
installs by default. Without them the ONNX embedding models and the pilot router
degrade silently at runtime.

When the current install was built from a local directory (detected via PEP 610
`direct_url.json`), the command prints a note naming that directory and
`scripts/install_source.sh` before proceeding. It is informational only: it
never prompts, blocks, or changes the exit code. `--json` reports the same as
`sourceDirectory` (`null` for a release install).

On **Windows** the managed gateway is stopped *before* the installer runs and
started again afterwards. Windows refuses to replace a file a live process holds
open, and the managed gateway runs the tool venv's own interpreter — leaving it
up is what produced `Access is denied` on
`…\uv\tools\use-agent-os\Scripts`, and a half-replaced directory with
`agentos` no longer on PATH. `--no-restart` keeps its promise not to touch the
gateway, so an upgrade with that flag can still hit the lock. POSIX is
unchanged: files are replaced under the running gateway, which is restarted
afterwards.

If the installer is refused anyway, the failure names the recovery instead of
only echoing the installer's error: stop the gateway and close every other
AgentOS process, re-run the printed command from a fresh terminal, and — if
`agentos` is then not found — put uv's tool bin directory back on PATH with
`uv tool update-shell`.

Exit codes: **0** success (upgraded + verified, or `--check`/`--dry-run`);
**3** this install method needs a manual command (printed); **1** the upgrade
failed, timed out, the post-restart version could not be verified, or the data
check failed; **2** an invalid `--source`.

Config migrations run at gateway start and write a timestamped backup before
rewriting any file, so `~/.agentos/` config and data are safe across upgrades.

### Version skew

Commands that talk to the gateway compare the CLI and gateway versions once per
run:

- **Gateway older than the CLI** (normal right after an upgrade, before a
  restart) — prints a warning on stderr, never blocks.
- **Gateway newer than the CLI** (you downgraded the CLI, or drive a newer
  gateway from a stale environment) — **refused**, because a newer gateway may
  have written config with a newer schema. Fix by upgrading the CLI or
  restarting the gateway from this environment; override in an emergency with
  `AGENTOS_ALLOW_VERSION_SKEW=1`.

### Update notifications

On gateway-connected commands the CLI checks PyPI at most once every 24h and,
if a newer release exists, prints a one-line notice on stderr. Similarly, the Web UI
queries the gateway on connection and displays a dismissible banner if an update is available.
The check is suppressed on non-interactive CLI runs (no TTY) and in CI. Control it with:

- `updates.notify = false` in `agentos.toml` (or the setup UI's Finish step) —
  turns the notices off entirely (both CLI and Web UI).
- `AGENTOS_NO_UPDATE_NOTICE=1` — silences it for a single run/session.

See [`configuration.md`](configuration.md#update-notifications).

## Configuration Commands

Provider and router:

```sh
agentos onboard
agentos onboard status
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
agentos configure router --router recommended
agentos providers list
agentos providers configure openrouter
agentos providers status
agentos models list
agentos models list --provider openrouter
agentos models list -c vision -c reasoning
agentos models list --json
```

`agentos configure router` **without** `--router` (and the router step of
`agentos onboard`) opens a Mode selector: **Local ML — English-optimized
(Pilot)** (default), **Smart routing (LLM-based)**, **Jev cloud classifier
(typesafe.ai, experimental)**, or **Off**. Passing `--router <mode>` is the
non-interactive form: it writes that tier profile and saves without asking
anything. Picking the Jev mode asks for a TypeSafe API key (leave it blank to
use `TYPESAFE_API_KEY` from the env store, set with `agentos env set
TYPESAFE_API_KEY`, which prompts for the value), verifies it with one test
call, and saves. A key typed at the prompt is written to `config.toml` under
`[agentos_router.jev]` (redacted on every public surface, and omitted whenever
it equals `$TYPESAFE_API_KEY`). To skip the wizard entirely:
`agentos config set agentos_router.strategy jev`. Jev sends the current turn
text to typesafe.ai; see
[`features/agentos-router.md`](features/agentos-router.md#the-jev-strategy).

`providers status` includes a `circuit` column with the active provider's
failover circuit-breaker state (`closed`, `half_open`, or `open (42s)`); see
[`providers-and-models.md`](providers-and-models.md#provider-health-circuit-breaker).

`agentos models list` queries the running gateway to inspect available models,
their context windows, supported capabilities, and per-1k token input/output
pricing. Filter results with `--provider <name>` or required `--capability` /
`-c <capability>` (repeatable). Use `--json` for structured output.

Provider-specific setup examples, including OpenCAP and Surplus Intelligence,
live in [`providers-and-models.md`](providers-and-models.md).

Search:

```sh
agentos search list
agentos search configure duckduckgo
agentos search query "latest AgentOS release"
agentos configure search --search-provider duckduckgo
```

X (Twitter) search — a separate xAI-backed tool, not a `web_search` backend.
With a SuperGrok / X Premium+ subscription, sign in instead of using a key:

```sh
agentos auth login xai      # device-code flow; preferred over XAI_API_KEY
agentos auth status         # never prints a token
agentos auth logout xai

agentos auth login xai --no-wait --json 2>/dev/null   # start, print link + code, exit
agentos auth login xai --resume --json 2>/dev/null    # exit 0 done, 3 not yet, 1 failed
```

```sh
agentos onboard catalog x-search
agentos configure x-search --api-key-env XAI_API_KEY
agentos configure x-search --x-search-model grok-4.5 --x-search-reasoning-effort low
agentos configure x-search --no-x-search-enabled
```

The `x_search` tool stays hidden from the agent until an xAI credential is
reachable. See [`x-search.md`](x-search.md).

Image generation:

```sh
agentos configure image --image-provider openai --primary openai/gpt-image-1 --api-key-env OPENAI_API_KEY
agentos configure image --no-image-enabled
```

Memory embedding:

```sh
agentos configure memory --memory-provider local --onnx-dir ~/.agentos/models/embeddings/google-embeddinggemma-300m
agentos configure memory --memory-provider openai --model text-embedding-3-small --api-key-env OPENAI_API_KEY
```

Channels:

Built-in channel types are `discord`, `email`, `slack`, and `telegram`; `agentos
channels types` is the authoritative catalog. On upgrade, config entries for
retired built-in channel types are removed only after AgentOS creates the
normal secure config backup.

Slack webhook entries accept `--field webhook_path=/slack/team-a/events`
when added with `channels add slack`. An omitted or empty field selects the
automatic path: the first enabled webhook account in config order keeps
`/slack/events` regardless of how many other webhook accounts are enabled, so
adding a second account never changes an already-configured account's Request
URL; every other enabled webhook account with no explicit path gets
`/slack/events/<account_name>`. Disabled and Socket Mode entries do not count,
or count as "first". An explicit path takes precedence. Configure each Slack
app's Request URLs to match its path. Duplicate webhook paths with overlapping
HTTP methods are rejected at gateway startup. See
[Slack modes](channels.md#slack-modes).

```sh
agentos channels types
agentos channels describe telegram
agentos channels native-commands telegram
agentos channels native-commands slack --request-url https://agent.example/slack/events
agentos channels add telegram --name personal
agentos channels add email --name inbox \
  --field imap_host=imap.example.com --field imap_username=agent@example.com \
  --field imap_password=<app-password> --field smtp_host=smtp.example.com \
  --field from_address=agent@example.com --field allowed_senders=you@example.com
agentos channels list
agentos channels status
agentos channels pairing list personal
agentos channels pairing approve personal ABCD2345
agentos channels pairing deny personal <telegram-user-id>
agentos channels pairing revoke personal <telegram-user-id>
agentos channels enable personal
agentos channels disable personal
agentos channels restart personal
agentos channels remove personal
```

`native-commands` prints the native platform payload derived from the same
channel command registry used for text `/command` dispatch. Telegram and
Discord menus synchronize when their adapters start. Slack also synchronizes
at startup when its channel entry has `app_id`, a short-lived app configuration
`manifest_token`, and `command_request_url`. Otherwise import the exported
Slack manifest fragment manually; its `--request-url` must point to the
gateway's Slack webhook endpoint.

Telegram direct messages always require pairing. Pairing is binary
(`unpaired`/`paired`), with no admin or owner tier. Groups are disabled by
default and require an explicit group chat ID, a paired sender, and—by
default—a bot mention. Any connected Control client may approve, deny, or
disconnect a pairing.

### Platform-Native Interactive Approvals

Platform-native interactive tool approvals (such as Slack block actions, Telegram inline keyboard callbacks, and Discord message components) allow operators to approve or deny gated tool executions directly using interactive buttons in their messaging app.

For security, interactive approvals are:
- **Restricted to Direct Messages (DMs)**: Interactive approval prompts are only sent in channel DMs, not group/channel chats, ensuring they cannot be triggered or visible to unauthorized participants in a shared room.
- **Access Gated**: Each button click/interaction verifies that the clicker's sender ID is paired and authorized under the channel's access policy. Clicking by an unpaired or unauthorized user is dropped and rejected.
- **Session Bound**: Approval tokens are strictly bound to their originating chat session key. A click received from a different chat context or user session will mismatch and be ignored.

Raw config:

```sh
agentos config get llm.provider
agentos config set port 18791
```

Without `--config`, `config set` prints the `export AGENTOS_GATEWAY_…` line
that applies the setting; with `--config <path>` it writes the file. Both
validate the value first, so a value the gateway would refuse (`tools.profile
bogus`) is reported as `Invalid value for …` here rather than when the
gateway starts. A gateway started with an invalid setting reports each bad
key on one line, naming the environment variable when one supplies it.

A long-lived gateway keeps per-session state in memory — stream replay
buffers, usage scopes, plan-mode flags, approval elevations. Every one of
those sits behind a shared bounded registry whose ceilings are config keys:

```sh
agentos config set registry_session_max_entries 512
agentos config set registry_cache_max_entries 512
agentos config set registry_cache_ttl_seconds 900
```

`registry_session_max_entries` bounds session-scoped state,
`registry_cache_max_entries` and `registry_cache_ttl_seconds` bound the
time-scoped caches.

Session state is normally dropped the moment a session is deleted, aborted or
completed; the ceiling is the backstop for sessions that never emit a terminal
event. Raise `registry_session_max_entries` on a gateway that runs many
simultaneous sessions.

For Ollama models that do not reliably support native tool calls, set
`tools.enabled = false` in the config file to run in plain-text mode. Keep it
enabled for tool-capable cloud models such as `glm-5.2:cloud`; the Ollama
provider preserves native tool-call history between turns.

More detail:

- [`configuration.md`](configuration.md)
- [`providers-and-models.md`](providers-and-models.md)
- [`search.md`](search.md)
- [`channels.md`](channels.md)

## Environment Variables

`agentos config` edits the TOML config. `agentos env` edits `~/.agentos/.env`,
which is where OS environment variables live — the credentials skills and
external binaries read, and provider keys you would rather not keep in the
config file.

```sh
agentos env list                       # every variable AgentOS knows about
agentos env list --missing             # only the ones that are not set
agentos env list --category skill      # provider | search | image | audio | memory | skill | custom
agentos env get OPENAI_API_KEY         # state and description, value masked
agentos env get OPENAI_API_KEY --reveal
agentos env set OPENAI_API_KEY --stdin # value read from stdin
agentos env import GITHUB_TOKEN         # copy from a tool that already has it
agentos env unset OPENAI_API_KEY
```

`agentos env import` covers the case where the credential is not really
missing. If you have run `gh auth login`, AgentOS can see that the GitHub CLI
holds a token and copy it in rather than asking you to go find one; `agentos
env list` marks such variables. Nothing is imported without you asking — a
token you granted to another tool is not automatically something an agent
should get. The copy does not follow that tool's own rotation, so re-run the
import after rotating.

Values are never printed unless you ask for them with `--reveal`, which
prompts first. Prefer `--stdin` or the interactive prompt over `--value`: a
value passed as a flag lands in your shell history and in the process list.

When the gateway is running, the change applies to it immediately, so a skill
that needed the variable becomes eligible without a restart. When no gateway
is running the file is written directly and the command says the value applies
at next start. Provider keys always need a restart to take full effect,
because the client was constructed at boot with the previous value — the
command tells you when that is the case.

Names that steer subprocess execution (`PATH`, `LD_PRELOAD`, `PYTHONPATH`,
`EDITOR`, …) or AgentOS runtime posture (`AGENTOS_AGENT_PERMISSIONS`,
`AGENTOS_GATEWAY_TOKEN`, `AGENTOS_STATE_DIR`, …) or outbound routing
(`HTTP_PROXY`, `AGENTOS_LLM_PROXY`, `AGENTOS_TRUST_ENV`, … — proxy names in
any casing) are refused, so this surface cannot be
used to widen what the agent is allowed to do. Edit `~/.agentos/.env` by hand
if you genuinely need one of them. Variables already
set that way keep working; only writing through AgentOS is gated.

What `agentos env list` knows about comes from three places: the setup specs
of providers the runtime can actually drive (a provider catalogued for the
setup UI with no client behind it -- Exa, Perplexity, and a number of LLM
vendors -- contributes nothing, so its key is not offered as "needed"),
built-in tools that read a variable directly (`web_fetch`'s
`FIRECRAWL_API_KEY`), and `requires.env` in installed skill manifests. A skill
entry declared with `required: false` unlocks a feature of the skill when set
-- an extra engine, say -- and its absence does not hide the skill. Anything
present in `.env` that none of those declare is listed as `custom`.

If `agentos env list` reports a variable as coming from `process env`, the
shell that started the gateway exported it and that value wins over the file.
Editing the file will not change anything until the export is removed.

Shell commands the agent runs inherit most of this environment, but not the
gateway token or the sandbox guard switches, and `execute_code` forwards only a
small allowlist plus what a skill declares. See
[Credentials and child processes](configuration.md#credentials-and-child-processes).

Read:

- [`configuration.md`](configuration.md)

## Skills

```sh
agentos skills init <name>
agentos skills init <name> --description "A custom skill description" -t "trigger1" -t "trigger2" --with-script
agentos skills list
agentos skills list --json
agentos skills search pdf
agentos skills view pdf-toolkit
agentos skills install <skill-name>
agentos skills install <skill-url> --source bankr
agentos skills install <skill-url> --source aeon
agentos skills update --all
agentos skills uninstall <skill-name>
agentos skills publish <path-to-skill>
agentos skills publish <path-to-skill> --repo <owner/repo>
agentos skills tap list
agentos skills tap add <owner/repo>
agentos skills tap remove <owner/repo>
```

`agentos skills init <name>` initializes a new custom skill template.
- `--description` / `-d` provides a description of the skill.
- `--trigger` / `-t` registers activation trigger terms (repeatable).
- `--target-dir` / `-p` specifies the target parent directory. If omitted, the tool resolves to the highest precedence existing layer directory in the workspace/personal layers list.
- `--with-script` scaffolds an executable script `scripts/run.py` template and entrypoint command configuration.
- `--force` / `-f` forces overwrite of generated files without purging the parent folder.

`agentos skills publish <path-to-skill>` validates the skill directory and
publishes it. `--repo` / `-r <owner/repo>` targets the repository the PR
goes to; a failed publish prints `Failed:` and exits 1.

`agentos skills tap` manages custom skill source repositories (taps) for
teams that keep their own skill catalog: `tap list` shows the registered
taps, `tap add <owner/repo>` registers one, `tap remove <owner/repo>`
removes it. See [`features/skills.md`](features/skills.md#manage-skill-sources).

The `skills list` table is unchanged: name, layer, eligible, description.
`--json` carries more, and now reports the same facts the Web UI shows for the
same skill instead of a separate, thinner answer:

| Key | What it says |
| --- | --- |
| `layer` | where the files are — `bundled`, `managed`, `personal`, `project`, `workspace`, `extra` |
| `acquisition` | how the skill got there: `kind` is `shipped`, `hub`, or `local`, plus `source_id`, `author`, `identifier`, `version`, `installed_at`, `source_trust`, `scan_verdict`, and the `removable` / `updatable` booleans |
| `publisher` | `{id, name, url, logo}`, all empty strings when the skill is unbranded. Only publishers on an allowlist inside AgentOS resolve to a name; a skill cannot brand itself by writing one into its manifest |
| `provenance` | unchanged, and independent of `publisher` — where the text came from and under what licence |
| `status` | `ready`, `needs_setup`, or `not_declared`, alongside a `disabled` boolean and a `status_detail` line |

`acquisition.removable` is the honest answer to "can `agentos skills uninstall`
remove this", not a restatement of the layer: a hub install whose recorded path
no longer matches the configured `skills.managed_dir` reports `false`, while
`updatable` stays `true` because an update re-fetches by identifier.

`status` answers "can this run". `ready` means the manifest declared
requirements **and** every one is satisfied; `not_declared` means there was no
`requires:` block to check. Both run — the split records only whether AgentOS
verified anything, which is why the Web UI shows them under one **Ready**
count and leaves the distinction to `status_detail`. `needs_setup` covers a
missing binary, a missing required env var, a wrong OS, and a skill switched
off via `skills.disabled` / `skills.enabled`; only the `disabled` boolean tells
the last one apart, and it is the only one no install will fix.

A required env var must be **non-blank** to count. `export ORACLE_KEY=` leaves
the variable set but empty, which no API key, token, or path survives, so it
reports as missing rather than as satisfied.

`acquisition.author` is an attribution string, not an identity. It is whatever
the catalog row credited — a handle a publisher chose — so it passes through no
allowlist and must never be rendered with a logo or read as a trust signal;
`publisher` is the only field that answers "who vouches for this". It is empty
only when it would repeat the resolved brand, so a partner skill is credited
once rather than twice — a *different* credit survives. That is the
wallet-published case: a skill written from a wallet on bankr.bot but named in
the wheel's user-skill allowlist carries Bankr's `publisher` and the author's
handle (e.g. `@igoryuzo`) as its `author`.

There is deliberately **no `availability` key** in CLI output. Whether the
agent is currently being offered a skill depends on a chat session's tool
surface, which a CLI process does not have; the gateway's `skills.list` and the
Web UI answer that instead. An absent key means "not computed", not "not
offered".

Read:

- [`features/skills.md`](features/skills.md)

## Sessions and History

```sh
agentos sessions list
agentos sessions list --search api-refactor    # match name, key, subject or model
agentos sessions list --agent main --status done   # also --channel, --since
agentos sessions show <session-key>
agentos sessions rename <session-key> "api-refactor"
agentos sessions rename <session-key> --clear  # drop the custom name
agentos sessions resume <session-key>
agentos sessions abort <session-key>
agentos sessions export <session-key>
agentos sessions delete <session-key>
```

Every filter on `sessions list` runs client-side over the recent history rather
than over the page `--limit` would show, so `--limit` bounds how many matches
are printed, not how far back the filter looks.

Sessions are auto-named. `rename` gives one a human-readable label that shows
up in `sessions list`, in the chat toolbar, and in the Web UI session list, and
that `--search`, `resume`, and `show` all accept in place of the key. Inside a
chat, `/rename <name>` does the same for the session you are in (no name
clears it). Names are trimmed, collapsed to one line, and capped at 120
characters.

## Projects

```sh
agentos projects list
agentos projects create "Token research" --knowledge "Shared context here"
agentos projects create "Token research" --knowledge-file notes.md
agentos projects show <project-id>
agentos projects update <project-id> --name "New name" --knowledge-file notes.md
agentos projects move <session-key> <project-id>   # 'none' detaches
agentos projects delete <project-id>               # sessions survive, detached
```

A project groups chat sessions across agents and carries a free-form
**knowledge** text (capped at 24,000 characters — the same ceiling the
per-turn injection applies, so everything that saves reaches the prompt in
full). Every session in the project
gets that knowledge injected into its system prompt as a `Project Knowledge`
block — edit it and the next turn of every member session picks it up.
Sessions of any agent can join the same project (the `--agent` on `create`
only sets the default agent for new chats in the project). New sessions can
start inside a project (`agentos chat` sessions join via `projects move`, the
Web UI has a "New chat in project" button), and deleting a project never
deletes sessions: they just detach and stop receiving the knowledge. Agents can manage projects from prompting through the
`projects_create` / `projects_list` / `projects_update` /
`projects_move_session` tools, and `session_search scope=project` searches
only sibling sessions of the calling session's project. The tools are scoped
to the calling session: `projects_update` edits only the session's own
project, `projects_list` returns knowledge text only for that project, and
`projects_move_session` moves only the calling session — everything else
stays on the CLI/Web UI surface.

Read: [`sessions.md`](sessions.md)

## Wallets and trading

```sh
agentos wallet status
agentos wallet setup --mode auto            # once; auto = password in ~/.agentos/wallets/unlock.key (0600)
agentos wallet setup --mode manual          # or: unlock per gateway session, keys only in RAM
agentos wallet unlock / lock
agentos wallet create --label main
agentos wallet import --label cold --private-key-stdin < key.txt
agentos wallet import --label cold --keystore wallet.json
agentos wallet export <addr> --keystore --out backup.json
agentos wallet export <addr> --private-key   # prints the raw key; always asks the vault password
agentos wallet list / rename <addr> <label> / primary <addr> / remove <addr> --yes|-y
agentos wallet balances [<addr>] [--chain base|robinhood] [--refresh] [--hidden] [--json]   # ledger view; --refresh re-reads the chain first (throttled to once per 10 s per wallet); --hidden lists junk tokens too

agentos trade status                        # provider, API key, chains, limits, vault state; --json adds ledgerRepair ("full sync required") after a ledger repair migration
agentos trade provider                      # show the swap provider (aggregator | uniswap)
agentos trade provider uniswap              # switch it (= config set trading.provider uniswap)
agentos trade probe [--provider aggregator|uniswap] [--api-key <key>]   # reachable? key valid? (--json exits 1 when not ok; --api-key is operator-only)
agentos trade tokens --chain robinhood AAPL # search; verified Stock Tokens are marked ✓
agentos trade quote --chain base --in ETH --out USDC (--amount 0.01 | --usd 5) [--wallet <addr>] [--slippage <pct>]
agentos trade swap  --chain base --in ETH --out USDC --amount 0.01 --wait [--wait-seconds 1..900] [--slippage <pct>]
agentos trade swap  --chain base --in ETH --out USDC --usd 5     # "$5 of ETH": the engine sizes it at the current price
agentos trade swap  --chain robinhood --in ETH --out <addr> --amount 0.01 --wallet <a> --wallet <b>   # Robinhood Chain: --amount (no native USD price yet → --usd may answer trading.unpriced); bare USDC resolves to lookalikes there, use the verified address
agentos trade swap  --chain base --in USDC --out ETH --amount 20 --all-wallets --note "DCA" [--as-agent]   # the daily cap is per wallet: this spends up to N caps
agentos trade swap  --chain base --in USDC --out ETH --amount 20 --client-id dca-eth-$(date +%Y%m%dT%H%M)   # idempotency key: one id per intended order, the same id on every retry; minute resolution so sub-daily jobs never collide
agentos trade swap  --chain base --in ETH --out USDC --amount 0.01 --expected-out-raw <quote.expectedOutRaw> --min-out-raw <quote.minOutRaw>   # pin the fill to the quote shown; worse than 2× slippage → trading.price_moved
agentos trade orders [--status awaiting_approval] [--wallet <addr>] [--kind swap|send|revoke] [--limit N]
agentos trade order <id> --wait --wait-seconds 600 / approve <id> / reject <id> [--reason <text>]   # --wait-seconds without --wait returns at once
agentos trade send --chain base --token USDC --to <addr> --amount 25 [--wallet <addr>] [--note <text>] [--client-id <id>] [--wait] [--wait-seconds 1..900] [--as-agent]
agentos trade send --chain base --token ETH --to <a> --to <b> --usd 5        # multisend: one batch, $5 of ETH to each
agentos trade send --chain base --token USDC --to <a>=10 --to <b>=20 --file recipients.txt   # ADDR=AMOUNT per --to; file lines 'ADDR' or 'ADDR,AMOUNT'
agentos trade allowances [--chain base|robinhood] [--wallet <addr>] [--full] [--wait/--no-wait] [--wait-seconds 1..3600]   # live ERC-20 allowances, spender labels, exposure; --wait polls until the scan has caught up
agentos trade revoke --chain base --token <addr> --spender <addr> [--wallet <addr>] [--note <text>] [--wait] [--wait-seconds 1..900]   # approve(spender, 0)
agentos trade decode --chain base <txhash> / --data <0x…> [--to <addr>]   # what a transaction called and what moved
agentos trade network [--fresh]             # head block, block age, gas, RPC latency and health per chain
agentos trade history [--wallet <addr>] [--chain base|robinhood] [--kind swap|deposit|withdraw|gas|approval] [--limit N] [--hidden]
agentos trade portfolio [--wallet <addr>] [--hidden]   # holdings, cost basis, realized + unrealized PnL; --hidden lists junk tokens too
agentos trade hide --chain base <addr> / unhide --chain base <addr>   # your call on a token's visibility; the engine never reverses it
agentos trade sync [--wallet <addr>] [--full]   # re-read the chain into the ledger; --full rebuilds it (operator-only) — run it once after an upgrade when `trade status` shows ledgerRepair
agentos trade limits [<addr>]               # guardrails + today's agent spend (default: the primary wallet)
```

Every command takes `--json`. On success the JSON is on stdout; on failure
stdout is empty and stderr carries `{"error": {"code": "…", "message": "…"}}`.
Exit codes: 1 = gateway or provider error (`GATEWAY_UNAVAILABLE`,
`trading.*`), 2 = bad input (`INVALID_ARGUMENT` — e.g. `--amount` and `--pct`
together, `--pct` outside `(0, 100]` — `TOKEN_NOT_FOUND`, `TOKEN_UNVERIFIED`,
`TOKEN_AMBIGUOUS`, `CONFIRMATION_REQUIRED`), 3 = conflict (`CONFLICT`,
`VERSION_SKEW`).

Wallets live in the engine's **vault** (`~/.agentos/wallets/`, keystore v3
files encrypted with one vault password). Nothing here is tied to an OS
keychain: `auto` mode keeps the password in `unlock.key` (mode 0600) so the
gateway unlocks at boot and the agent can sign unattended; `manual` mode
keeps keys only in the gateway's memory after `agentos wallet unlock`. Export
always asks the vault password. Passwords are read from a hidden prompt or
`AGENTOS_WALLET_PASSWORD` (`AGENTOS_KEYSTORE_PASSWORD` for an imported
keystore) — never from the command line.

Swaps run on Base (8453) and Robinhood Chain (4663) through one of two
providers. The **AgentOS Aggregator** (the default,
`trading.provider = "aggregator"`, served at `https://agg.useagentos.dev`)
needs no key and no account: one GET returns the price *and* the unsigned
calldata, including the ERC-20 approval when one is needed. It never signs
and never broadcasts — your wallet does both — and it charges 20 bps on the
swap, reported back in the quote. **Uniswap** (`agentos trade provider
uniswap`) is the fallback and needs a key: `agentos config set
trading.uniswap_api_key <key>`, or Settings › Trading in the desktop app.

29 of the 34 listed tokens on Robinhood Chain (the tokenised stocks — AAPL,
TSLA, SPY and the rest) cannot be routed at all: the aggregator answers
`trading.token_not_tradeable`, a legal refusal upstream that no retry, size,
address or time of day changes. ETH, WETH and USDG trade normally there.
Robinhood Chain has no native USD price feed yet, so `--usd` may be refused
with `trading.unpriced` (size with `--amount`), and the bare symbol `USDC`
resolves to unverified lookalikes there (`TOKEN_UNVERIFIED`): use an
address `agentos trade tokens --chain robinhood …` marks `verified: true`.

`--in`/`--out` take `ETH`, an address, or a symbol; a symbol must resolve to
exactly one *verified* token or the command exits 2 (`TOKEN_AMBIGUOUS`,
`TOKEN_UNVERIFIED`, `TOKEN_NOT_FOUND`). Amounts are human units; `--pct`
accepts fractions, and `--pct 100` on ETH keeps about 0.001 ETH back for
gas. A quote does not check balance or gas — the swap does
(`trading.insufficient_balance`) — and carries `expiresAt` (about 20 s for
the aggregator, 30 s for Uniswap).

Guardrails apply to **agent-initiated** swaps, and the **gateway** decides
who is an agent: a shell spawned by an agent turn carries an agent token
(`AGENTOS_AGENT_TOKEN`), a `cron --script` job carries one too, any
connection opened while an agent shell is running counts as the agent's,
and a connection that presents nothing is the agent's as well. The operator
proves themself with a secret: the desktop app hands the gateway one at
spawn, and the gateway writes its own to
`~/.agentos/wallets/operator.secret` (mode 0600, rotated at every boot). The
CLI reads that file on its own when `AGENTOS_AGENT_TOKEN` is not set and the
gateway is local; against a remote gateway it presents nothing and gets the
agent's rules. An agent-bound connection is an agent whatever it declares
(`--as-agent` only forces the agent rules for a person). For the agent: an
order above `trading.approval_threshold_usd` (default 100) or above
`trading.agent_max_price_impact_pct` (default 5; "price impact" is the
price vs reference, venue fee and feed skew included) waits as
`awaiting_approval` for `trading.approval_ttl_seconds` (15 minutes); an
order that would push a wallet past `trading.daily_cap_usd` (default 1,000
per wallet per calendar day — `--all-wallets` spends up to N caps — with an
order counted as max(in, out) in USD, orders in flight included; 0 switches
agent swaps off) is
rejected; `--slippage` above `trading.agent_max_slippage_pct` (default 5) is
refused with `trading.slippage_too_high`. `agentos trade approve` /
`reject`, `hide` / `unhide`, `probe --api-key`, `sync --full` and every
vault command except `status`, `list` and `balances` fail from an agent's
connection with `trading.operator_required`; `config set` on any `trading.`
key is refused from an agent too (the gateway rejects the write as an
invalid request). Those are the user's actions, in the app or their own
terminal. `wallet status` and `trade status` leave out `vaultPath` for an
agent. Swaps typed by a person are neither queued nor capped; if the price
moves more than twice the slippage between quote and send they fail with
`trading.price_moved` instead. `swap` also accepts the quote's
`expectedOutRaw` / `minOutRaw` (`--expected-out-raw`, `--min-out-raw`) and
refuses with `trading.price_moved` when the fill would be more than 2× the
slippage worse than that quote. `--wait --wait-seconds N` blocks until each
order settles (`confirmed`, `failed`, `rejected`, `expired`) or N seconds
pass; `--wait-seconds` alone, without `--wait`, returns at once. A
`submitted` order survives a gateway restart and is marked `failed` after 6
hours without a receipt. `--client-id <id>` on `swap` and `send` is an
idempotency key: a second call with the same id returns the existing order
instead of placing another, so a retry after a timeout cannot trade twice
— one id per intended order, the same id on every retry of it. After
upgrading, if `agentos trade status --json` shows `ledgerRepair`, run
`agentos trade sync --full` once. A swap's target and the
approval's spender are pinned to the contracts the desk knows for the
provider (a quote naming any other address is refused, not signed); the gas
limit is the provider's or the estimate plus 20 %, and the order is refused
up front when the wallet cannot cover value plus gas at the quoted fee.
`--note` is stored as shown to the approver: control and bidi characters
are dropped, whitespace collapses, and it is cut at 240 characters.

Sends and revokes share the order pipeline (`kind` is `swap`, `send` or
`revoke`). A send moves ETH by value or an ERC-20 by `transfer`, one plain
transaction per recipient; several `--to` (up to 200, from flags or a
`--file`) make one **batch** with a shared `batchId` that is judged,
approved, rejected and reported as one. Guardrails differ from swaps on
purpose: an **agent-initiated** send or revoke always waits for the user's
approval — there is no threshold under which the agent sends alone — while
the daily cap still counts the batch's total; a send typed by a person runs
at once. Each leg is booked as a `withdraw` entry under its order. `trade
allowances` scans the wallet's own `Approval` logs incrementally, reads
every remembered (token, spender) allowance live, and shows what is at
stake (`exposureUsd` = the smaller of the allowance and the balance, in
dollars); `trade revoke` sends `approve(spender, 0)`. `trade decode` names
the function behind a hash or calldata (ERC-20, WETH, Permit2 and the
Uniswap routers are known; anything else is reported as its selector, never
guessed) and lists the receipt's transfers and approvals with token
metadata. `trade network` reads each chain's head block and gas and flags a
head older than a minute — the sign of an RPC that is behind.

`[trading]` config keys (each also an environment variable with the
`AGENTOS_TRADING_` prefix): `enabled`, `provider` (`aggregator` |
`uniswap`), `aggregator_base_url` (default
`https://agg.useagentos.dev`), `uniswap_api_key`, `uniswap_api_key_env`
(default `UNISWAP_API_KEY`), `rpc_urls` (chain id → JSON-RPC URL),
`approval_threshold_usd`, `daily_cap_usd` (0 = agent swaps off),
`approval_ttl_seconds`, `agent_max_price_impact_pct`,
`agent_max_slippage_pct`, `default_slippage_pct` (unset = provider auto),
`unlock_mode` (`auto` | `manual`), `sync_interval_seconds`,
`price_ttl_seconds`.

Read: [`features/trading.md`](features/trading.md)

## Memory

```sh
agentos memory status
agentos memory index
agentos memory list --source all
agentos memory ingest /path/to/docs
agentos memory curated get --target memory
agentos memory curated add "Important project convention"
agentos memory curated remove "Important project convention"
agentos memory search "preference"
agentos memory show <path>
agentos memory embedding-download
agentos memory raw-fallbacks list
agentos memory raw-fallbacks show <path>
```

Read: [`features/memory.md`](features/memory.md)

## Durable Agents and Scheduling

```sh
agentos agents list
agentos agents add research --name Research --workspace /path/to/research
agentos agents delete research
agentos cron list
agentos cron add --every 1h --text "Summarize important updates" --name hourly-summary
agentos cron status <job-id>
agentos cron runs <job-id>
agentos cron output <job-id>
```

`--job-kind` picks what fires: `reminder` (delivers `--text` verbatim, no LLM),
`script` (runs a file, no LLM), `agent_turn` (the agent runs `--text` as a
prompt), or `system_event`. It defaults to `auto`, which is `reminder` for normal
targets — so the example above repeats that sentence hourly rather than
summarizing anything. Add `--job-kind agent_turn` to have the agent do the work.

### Running a script on a schedule, without a model

```sh
agentos cron add --every 5m --script watch-memory.sh --name memory-watchdog
agentos cron update <job-id> --script watch-disk.sh --workdir /srv/app
agentos cron add --every 15m --script watch_rss.py --name hn \
  --script-arg --url --script-arg https://news.ycombinator.com/rss
```

`--script` implies `--job-kind script` and resolves relative to
`~/.agentos/scripts/`; absolute paths, `~`, and `..` are refused, and so is a
symlink out of that directory. Subdirectories are allowed, and `{job_id}`
anywhere in the path is replaced with the created job's own id, so a job can own
a directory named after itself in one `add`. `.sh`/`.bash` run under bash,
anything else under python. `--workdir` sets the script's working directory; a
relative value resolves against the script's own directory, which is also the
default. `--script-arg` (repeatable) passes argv straight to
the script — never through a shell. Non-empty stdout is delivered verbatim, empty stdout is a silent
run, and a non-zero exit or `--timeout` delivers the error and fails the job.
Secrets are masked in the output, and the gateway token is withheld from the
child process. A script job runs under the agent's rules: the scheduler hands
it an agent token, so an `agentos trade` command inside it is an agent order
(threshold, daily cap, approval) and the operator-only commands fail in it.
The bundled `cron-watchers` skill ships scripts for RSS, JSON
endpoints, and GitHub repos that already follow this contract.

#### Seeing what the script did

A job scheduled from the CLI has no conversation attached, so "delivered
verbatim" has nowhere to deliver to: the stdout lands on the run record and the
chat stays empty. `--session-key` names the chat the job reports into — the run
itself stays isolated, only the output is mirrored there:

```sh
agentos sessions list                       # copy the key of the chat you want
agentos cron add --every 5m --script watch-memory.sh --name memory-watchdog \
  --session-key 'agent:main:webchat:<id>'
```

Either way `agentos cron runs <job-id>` shows each run's `Output` and
`Delivery` columns. The `Output` column is a 500-character preview, so the whole
list stays small no matter how much a job prints; `agentos cron output <job-id>`
prints one run's output in full (the most recent run, or `--run <run-id>` for an
older one — run ids come from `agentos cron runs --json`). A `Delivery` of
`fwd:no_session_target` is the scheduler saying the script printed something
that reached no conversation — add `--session-key`. Jobs created from the Web UI
or from a chat already carry their originating session, so their output shows up
in that chat without any extra flag.

Add `--script` to an `--job-kind agent_turn` job instead and it becomes a
pre-run collector: its stdout is handed to the agent as context, and a tick
where it prints nothing skips the turn entirely — no LLM call at all. See
[`scheduling.md`](scheduling.md).

No LLM runs, so no tokens are spent — but nothing reviews the script before it
executes either. It runs on this host as you, unattended, so treat
`~/.agentos/scripts/` as trusted as your shell profile. Only an interactive CLI
or Web caller can create one; the in-agent `cron` tool refuses `job_kind='script'`
from a channel.

### Announcing to a specific channel

`--announce --channel telegram --to <chat-id>` pins where a job reports, and
`--account`, `--no-deliver`, `--best-effort-deliver`, and `--webhook-url` cover the rest.
The in-agent `cron` tool accepts the channel case too, through a `delivery`
object — also restricted to an interactive CLI or Web caller, so a chat
participant cannot redirect a job into a room they were never in. Webhook
delivery and failure destinations stay CLI/Web/RPC-only. See
[`scheduling.md`](scheduling.md#naming-a-channel-recipient).

### Letting a cron job run shell-based skills

By default, cron jobs of kind `agent_turn` run elevated under the `bypass` mode
(controlled globally by `permissions.cron_default_mode`). This allows them to
run shell-based commands (like those in skills) without interactive approval prompts.

If you wish to opt out a job from elevated execution, pass `--no-elevated`:

```sh
agentos cron add --every 6h --agent main --no-elevated \
  --name "LP check" --text "Use the senior-unilp-manager skill to review my LP positions"
agentos cron list                       # the Elevated column shows the mode
agentos cron update <job-id> --elevated-mode bypass
```

Related flags: `--elevated` (which sets the job to explicitly run elevated), `--elevated-mode {bypass,full}`, and
`--tool-policy '<json>'` (`profile`, `allow`, `alsoAllow`, `deny` — can only
narrow the cron baseline). `profile` must be one of `coding`, `full`,
`memory_only`, `messaging`, `minimal`; omit the key to inherit rather than
inventing a name, since an unknown one is rejected when the job is written.
Elevation is only accepted on agent-turn jobs; reminders and system events
never run an agent turn with the job's tool policy.

**What you are accepting.** Every time the job fires, with nobody watching, an
LLM decides which shell commands run on this host as you, and they run — no
approval prompt, no sandbox, with your environment variables and API keys
passed through to the child process. If the skill signs transactions, an
unattended turn can sign and broadcast them. `write_file`, `git_commit`,
`apply_patch` and `execute_code` stay off the offered tool surface, but
`exec_command` reaches all of them, so treat that list as a default rather than
containment. Most importantly, anything the job reads from the network
(`web_fetch`, `web_search`, RPC responses, token metadata) is untrusted input
one reasoning step away from that shell.

Still enforced: the never-bypassable command denylist; the sensitive-path block
on *destructive* operations against `~/.ssh`, `.env*` and private keys, which
`bypass` keeps and `full` disables; workspace lockdown and write-deny globs; no
private-memory reads (force-denied for every cron caller, and no tool policy can
revive them); no `cron` tool, so the job cannot schedule or elevate another; and
no `message` tool, so output goes only where you configured delivery. Note the
sensitive-path block does not stop a *read* of a secret file — once
`exec_command` is on, secrets on disk are reachable.

A cron turn also never loads `USER.md`, so anything per-user the skill needs
(wallet address, chain, thresholds) has to come from the task text or the
environment.

Practical shape: one skill and one narrow task per elevated job, a tight
`--timeout`, `--session-target isolated`, delivery and a failure destination
configured, and if the skill has a dry-run/confirm handshake, keep cron on the
read half and leave broadcasts to an interactive session.

Read:

- [`agents.md`](agents.md)
- [`scheduling.md`](scheduling.md)
- [`approvals-and-permissions.md`](approvals-and-permissions.md)

## Sandbox Posture Controls

```sh
agentos sandbox status
agentos sandbox status --json
agentos sandbox bypass
agentos sandbox full
agentos sandbox on
agentos sandbox reset
```

`agentos sandbox status` shows the current sandbox posture (`on`, `bypass`, `full`), whether runtime sandboxing and security grading are active, and default permissions.

- `agentos sandbox on`: Restores the default sandboxed posture (`sandbox = true`, `security_grading = true`, `permissions.default_mode = "off"`).
- `agentos sandbox bypass`: Disables runtime sandboxing and auto-grants approvals except for sensitive paths (`permissions.default_mode = "bypass"`).
- `agentos sandbox full`: Disables runtime sandboxing and skips approval and sensitive-path gates (`permissions.default_mode = "full"`).
- `agentos sandbox reset`: Resets sandbox posture to AgentOS defaults (`bypass`).

Pass `--config <path>` to target an explicit configuration file. Changes require a gateway restart (`agentos gateway restart`) to apply to running processes.

Read: [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Cost, Diagnostics, and Replay

```sh
agentos context
agentos context --json
agentos cost
agentos cost savings
agentos diagnostics status
agentos diagnostics on
agentos diagnostics off
agentos replay --session <session-key> --turn <turn-id>
```

`agentos context` answers a different question from `agentos cost`: not what a
session spent, but what every request carries before the conversation starts.
Tool schemas dominate it — around 7,300 tokens on a stock install, charged on
every call in every turn — and the command prices each `[tools] profile` against
the current one so the trade is visible before you make it. A profile is fixed
for the session, so narrowing it does not disturb the prompt cache.

Every CLI command logs to stderr at `INFO` and above, so its output is only
the command's own. Set `AGENTOS_LOG_LEVEL=debug` to see the debug-level events
too (the gateway process keeps its own configured `log_level`, `DEBUG` by
default, and `agentos chat` its `WARNING`).

`agentos cost` aggregates and displays model usage and estimated cost reports from the gateway:

```sh
agentos cost [--by-model] [--json] [--csv]
agentos cost --start-date YYYY-MM-DD --end-date YYYY-MM-DD
agentos cost --agent-id <agent-id> --channel-type <channel-type>
agentos cost --tool-name <tool-name> --skill <skill-name>
agentos cost --export /path/to/export.csv
```

| Option | Purpose |
| --- | --- |
| `--by-model` | Group aggregate rows by model. |
| `--json` | Emit machine-readable JSON. |
| `--csv` | Emit machine-readable CSV. |
| `--start-date` | Filter by start date (YYYY-MM-DD). |
| `--end-date` | Filter by end date (YYYY-MM-DD). |
| `--agent-id` | Filter by agent ID. |
| `--channel-type` | Filter by channel type. |
| `--tool-name` | Filter by tool name. |
| `--skill` | Filter by skill name. |
| `--export` | Path to export results (JSON/CSV). |

### `agentos cost savings`

`agentos cost` answers "what did I spend"; `agentos cost savings` answers "what
did the [Pilot Router](features/agentos-router.md) save me". It reads the local
decision log (`~/.agentos/logs/decisions-*.jsonl`) rather than the gateway, so
it works with the gateway stopped:

```sh
agentos cost savings                                  # summary + per-route table
agentos cost savings --json
agentos cost savings --csv
agentos cost savings --start-date 2026-08-01 --end-date 2026-08-31
agentos cost savings --pdf ~/pilot-router-savings.pdf
```

| Option | Purpose |
| --- | --- |
| `--pdf` | Write a branded, shareable PDF report to this path. |
| `--json` | Emit machine-readable JSON. |
| `--csv` | Emit machine-readable CSV, one row per route pair. |
| `--start-date` | Filter by start date (YYYY-MM-DD, UTC, inclusive). |
| `--end-date` | Filter by end date (YYYY-MM-DD, UTC, inclusive). |
| `--log-dir` | Read a decision-log directory other than `~/.agentos/logs`. |

**Read the number correctly.** The baseline is *the most expensive model
configured in `[router.tiers]`* — what every routed turn would have cost had it
gone to your top tier — and not the model the request arrived with. Only input
tokens are priced, at list rates, so the figure is a floor rather than a
full-turn saving. The report covers routing alone: tool-result projection,
short-reply enforcement, prompt-cache hits and thinking mode are excluded, even
though the decision log carries them too. The `Requested` column names the
model the turn asked for, which is *not* the price comparison — a turn can be
routed back onto the requested model and still show a saving, because the top
tier is dearer than both.

Turns are logged only when the decision log is being written, so the window
starts at your oldest retained `decisions-*.jsonl` file.

Use diagnostics and replay when you need to understand why a turn behaved a
certain way. For Prometheus metrics (`/metrics`), OTLP trace export, and log retention
settings under `[observability]`, see [`configuration.md`](configuration.md#observability).

Read:

- [`usage-and-cost.md`](usage-and-cost.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)
- [`configuration.md`](configuration.md)

## MCP Server Bridge

```sh
agentos mcp-server run
agentos mcp-server run --gateway ws://localhost:18792/ws
```

Read: [`mcp-server.md`](mcp-server.md)

## Install Inventory

`agentos dist` emits `workspace-state.json` — a reproducible, versioned
inventory of the install for support, release QA, or environment
comparison:

```sh
agentos dist
agentos dist --output workspace-state.json
```

With no flags the payload prints to stdout. `--output` (`-o`) writes it to
the given file instead (creating parent directories) and prints the
resolved path. The payload (`schema_version`, `agentos_version`,
`python_requires`, `bundled_channels`, `bundled_tools`,
`gateway_defaults`) is derived only from installed package metadata plus
hard-coded constants — byte-identical per install, with no environment
values, paths, or secrets.

Read: [`operations.md`](operations.md#install-inventory)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
