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
| `agentos configure` | Reconfigure provider, router, channels, search, image generation, or memory embedding. |
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
| `agentos diagnostics` | Enable or disable runtime diagnostics logging. |
| `agentos replay` | Replay a recorded turn from the decision log. |
| `agentos migrate` | Import state from external agent runtimes. |
| `agentos models` | Inspect available models. |
| `agentos agents` | Manage durable agents. |
| `agentos mcp-server` | Run the AgentOS MCP server bridge. |
| `agentos dist` | Emit a reproducible workspace-state inventory. |
| `agentos reset` | Reset a session and flush memory synchronously. |

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
| `/clear` / `/reset` | Clear the current conversation context. |
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
installed, upgrades it, and — by default — restarts the managed gateway and
**verifies** the running gateway reports the new version before declaring
success (a "successful" upgrade that leaves the daemon on old code is the
common upgrade regret).

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

- **uv tool / pipx** — delegated automatically (`uv tool upgrade use-agent-os`
  / `pipx upgrade use-agent-os`), resolving the tool to an absolute path over a
  hardened PATH.
- **pip / editable / source checkout** — not faked: prints the exact manual
  command (e.g. `python -m pip install --upgrade "use-agent-os"`) and exits
  with a distinct code.

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

Search:

```sh
agentos search list
agentos search configure duckduckgo
agentos search query "latest AgentOS release"
agentos configure search --search-provider duckduckgo
```

Channels:

```sh
agentos channels types
agentos channels describe telegram
agentos channels native-commands telegram
agentos channels native-commands slack --request-url https://agent.example/slack/events
agentos channels add telegram --name personal
agentos channels list
agentos channels status
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

## Skills

```sh
agentos skills list
agentos skills search pdf
agentos skills view pdf-toolkit
agentos skills install <skill-name>
agentos skills update --all
agentos skills uninstall <skill-name>
```

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
agentos memory dream
agentos memory flush-session <session-key>
agentos memory repair list
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
```

Read:

- [`agents.md`](agents.md)
- [`scheduling.md`](scheduling.md)

## Cost, Diagnostics, and Replay

```sh
agentos cost
agentos diagnostics status
agentos diagnostics on
agentos diagnostics off
agentos replay --session <session-key> --turn <turn-id>
```

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
