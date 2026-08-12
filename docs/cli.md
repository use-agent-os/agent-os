# CLI Reference

The `agentos` CLI is the fastest way to configure, run, inspect, and
automate AgentOS.

Run:

```sh
agentos --help
agentos <command> --help
```

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
| `agentos sessions` | List, inspect, resume, abort, delete, or export sessions. |
| `agentos skills` | List, search, view, install, update, publish, and inspect skills. |
| `agentos memory` | Inspect and maintain memory. |
| `agentos channels` | Configure and inspect messaging channels. |
| `agentos providers` | Configure and inspect LLM providers. |
| `agentos search` | Configure and use web search. |
| `agentos sandbox` | Inspect or change default sandbox posture. |
| `agentos cron` | Manage scheduled AgentOS runs. |
| `agentos cost` | Inspect usage and estimated cost. |
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
| `/c0` … `/c3` | Pin the Pilot Router to a configured tier for this session. The pin appears in the bottom toolbar (e.g. `tier:c3`) and stays active until you exit, run `/auto`, or the hold expires. |
| `/auto` | Restore automatic Pilot Router routing (clears the tier pin). |
| `/help` | List the commands available on the current surface. |
| `/exit` / `/quit` | Leave the REPL. |

Router tier commands (`/c0` … `/c3`, `/auto`) are available in both gateway
and `--standalone` modes. Tiers not present in your `[agentos_router]`
config are rejected with a readable error. In `--standalone` mode the
router must be enabled in config; otherwise the command reports
"Pilot Router is disabled or unavailable."

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
| `--timeout` | Set total agent wall-clock timeout. |
| `--max-iterations` | Bound the model/tool loop. |
| `--max-provider-retries` | Bound transient provider retries. |
| `--length-capped-continuations` | Bound automatic continuations after length-limited provider output. |
| `--thinking` | Override reasoning level. |
| `--permissions` | Select restricted, bypass, or full permission posture. |
| `--transcript-path` | Write a JSONL transcript for automation. |
| `--usage-path` | Write usage JSON. |
| `--session-db-path` | Persist session replay across invocations. |

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
agentos upgrade                 # upgrade, restart the gateway, verify
agentos upgrade --check         # is a newer release available? change nothing
agentos upgrade --dry-run       # print the exact command that would run
agentos upgrade --no-restart    # upgrade only; leave the gateway on OLD code
agentos upgrade --timeout 900   # bound the upgrade subprocess (default 600s)
```

| Flag | Purpose |
| --- | --- |
| `--check` | Query PyPI for a newer release (5s timeout); offline prints `could not check (offline)`. Changes nothing. |
| `--dry-run` | Print the upgrade command that would run and whether the gateway would be restarted; touch nothing. |
| `--no-restart` | Upgrade the package but do not restart the gateway. Prints an unmissable warning that it still runs the old version; run `agentos gateway restart` yourself. |
| `--timeout` | Upgrade-subprocess timeout in seconds (default 600). On timeout the tool's process group is killed with recovery guidance — never a half-state. |
| `--config` | Target a specific config file for the gateway restart. |
| `--json` | Machine-readable output. |

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

Exit codes: **0** success (upgraded + verified, or `--check`/`--dry-run`);
**3** this install method needs a manual command (printed); **1** the upgrade
failed, timed out, or the post-restart version could not be verified.

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
if a newer release exists, prints a one-line notice on stderr. It is suppressed
on non-interactive runs (no TTY) and in CI. Control it with:

- `updates.notify = false` in `agentos.toml` (or the setup UI's Finish step) —
  turns the notice off entirely.
- `AGENTOS_NO_UPDATE_NOTICE=1` — silences it for a single run.

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
```

Provider-specific setup examples, including OpenCAP, live in
[`providers-and-models.md`](providers-and-models.md).

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

Channels:

Built-in channel types are `discord`, `slack`, and `telegram`; `agentos
channels types` is the authoritative catalog. On upgrade, config entries for
retired built-in channel types are removed only after AgentOS creates the
normal secure config backup.

```sh
agentos channels types
agentos channels describe telegram
agentos channels native-commands telegram
agentos channels native-commands slack --request-url https://agent.example/slack/events
agentos channels add telegram --name personal
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

Raw config:

```sh
agentos config get llm.provider
agentos config set gateway.port 18791
```

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
`AGENTOS_GATEWAY_TOKEN`, `AGENTOS_STATE_DIR`, …) are refused, so this surface
cannot be used to widen what the agent is allowed to do. Edit
`~/.agentos/.env` by hand if you genuinely need one of them. Variables already
set that way keep working; only writing through AgentOS is gated.

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
agentos skills list
agentos skills list --json
agentos skills search pdf
agentos skills view pdf-toolkit
agentos skills install <skill-name>
agentos skills install <skill-url> --source bankr
agentos skills update --all
agentos skills uninstall <skill-name>
```

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
`stock-premium-lp-manager` case: written from a wallet on bankr.bot but named in
the wheel's user-skill allowlist, so it carries Bankr's `publisher` and
`@igoryuzo` as its `author`.

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
agentos sessions show <session-key>
agentos sessions resume <session-key>
agentos sessions abort <session-key>
agentos sessions export <session-key>
agentos sessions delete <session-key>
```

Read: [`sessions.md`](sessions.md)

## Memory

```sh
agentos memory status
agentos memory index
agentos memory list
agentos memory search "preference"
agentos memory show <path>
agentos memory raw-fallbacks list
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
symlink out of that directory. `.sh`/`.bash` run under bash, anything else under
python. `--script-arg` (repeatable) passes argv straight to the script — never
through a shell. Non-empty stdout is delivered verbatim, empty stdout is a silent
run, and a non-zero exit or `--timeout` delivers the error and fails the job.
Secrets are masked in the output, and the gateway token is withheld from the
child process. The bundled `cron-watchers` skill ships scripts for RSS, JSON
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

### Letting a cron job run shell-based skills

A cron turn runs under a read-only tool allowlist, so a job that is shown a
skill can read `SKILL.md` and never carry it out — nearly every skill body is a
block of shell. `--elevated` opts one job out of that:

```sh
agentos cron add --every 6h --agent main --elevated \
  --name "LP check" --text "Use the senior-unilp-manager skill to review my LP positions"
agentos cron list                       # the Elevated column shows the mode
agentos cron update <job-id> --no-elevated
```

Related flags: `--elevated-mode {bypass,full}` (default `bypass`) and
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

## Cost, Diagnostics, and Replay

```sh
agentos context
agentos context --json
agentos cost
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

Use diagnostics and replay when you need to understand why a turn behaved a
certain way.

Read:

- [`usage-and-cost.md`](usage-and-cost.md)
- [`diagnostics-and-replay.md`](diagnostics-and-replay.md)

## MCP Server Bridge

```sh
agentos mcp-server run
agentos mcp-server run --gateway ws://localhost:18792/ws
```

Read: [`mcp-server.md`](mcp-server.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
