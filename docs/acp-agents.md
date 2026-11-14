# ACP Coding Harnesses

AgentOS can hand coding work to an external coding agent (Claude Code,
Codex, Gemini CLI, Cursor, GitHub Copilot, OpenCode and 20 more) and get
the result back as a normal child session. It talks to each one over the
[Agent Client Protocol](https://agentclientprotocol.com) (ACP): AgentOS is
the ACP *client*, the harness runs as an ACP *agent* subprocess speaking
JSON-RPC over stdio.

Compared with running `codex exec` or `claude -p` in a background shell,
the ACP route gives you:

- one structured event stream (text, tool calls, plans, usage) instead of
  scraped terminal text;
- harness permission prompts routed to the AgentOS approval queue (or
  answered by a policy you choose);
- resumable sessions, cancel, and a per-turn wall-clock limit;
- process leases, so no harness outlives the gateway;
- token and cost accounting on the child session (USD when the harness
  reports it; Claude Code does);
- a completion pushed back to the parent session with a `Stats:` line.

The design follows OpenClaw's `acpx` runtime (MIT); see
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

## Quick start

```sh
agentos acp list                       # every harness and how it would launch
agentos acp doctor claude              # CLI on PATH? adapter installed?
agentos acp doctor claude --probe      # spawn it and run ACP initialize (30 s cap)
agentos acp run claude "Summarize this repo in five bullets" --cwd ~/code/app
```

`agentos acp run` needs no gateway: it starts the harness on this machine,
sends one prompt, streams the reply and prints a stats line. From a chat,
ask the agent to delegate instead ("have Claude Code fix the failing test in
~/code/app"); it calls `sessions_spawn` with `runtime="acp"`.

## Supported harnesses

`id` is what you pass to `--agent` / `acp_agent`. Aliases such as
`claude-code`, `gemini-cli`, `github-copilot`, `cursor-agent`,
`factory-droid`, `kilo`, `kimi-cli`, `kiro-cli`, `qwen-code` and `grok`
resolve to the same ids.

| id | Harness | Default launch | Needs on PATH |
| --- | --- | --- | --- |
| `claude` | [Claude Code](https://claude.ai/code) | `npx -y @agentclientprotocol/claude-agent-acp@^0.91.0` (or `claude-agent-acp` when installed) | `claude` |
| `codex` | [Codex CLI](https://developers.openai.com/codex/cli) | `npx -y @agentclientprotocol/codex-acp@^2.2.2` (or `codex-acp`) | `codex` |
| `gemini` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `gemini --acp` | `gemini` |
| `cursor` | [Cursor CLI](https://cursor.com/docs/cli/acp) | `cursor-agent acp` | `cursor-agent` |
| `copilot` | [GitHub Copilot CLI](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-copilot-cli) | `copilot --acp --stdio` | `copilot` |
| `opencode` | [OpenCode](https://opencode.ai) | `npx -y opencode-ai acp` (or `opencode acp`) | - |
| `pi` | [Pi Coding Agent](https://github.com/earendil-works/pi) | `npx pi-acp@^0.0.33` (or `pi-acp`) | `pi` |
| `droid` | [Factory Droid](https://www.factory.ai) | `droid exec --output-format acp` | `droid` |
| `grok-build` | [Grok Build](https://docs.x.ai/build/overview) | `grok agent stdio` | `grok` |
| `iflow` | [iFlow CLI](https://github.com/iflow-ai/iflow-cli) | `iflow --experimental-acp` | `iflow` |
| `junie` | [Junie](https://github.com/openclaw/acpx/blob/main/agents/Junie.md) | `junie --acp=true` | `junie` |
| `kilocode` | [Kilo Code](https://kilocode.ai) | `npx -y @kilocode/cli acp` (or `kilo acp`) | - |
| `kimi` | [Kimi CLI](https://github.com/MoonshotAI/kimi-cli) | `kimi acp` | `kimi` |
| `kiro` | [Kiro CLI](https://kiro.dev) | `kiro-cli-chat acp` | `kiro-cli-chat` |
| `mcode` | MiniMax Code | `mcode acp` | `mcode` |
| `mux` | [Mux](https://mux.coder.com) | `npx -y mux@^0.28.0 acp` | - |
| `pool` | Poolside | `pool acp` | `pool` |
| `qoder` | [Qoder CLI](https://docs.qoder.com/cli/acp) | `qodercli --acp` | `qodercli` |
| `qwen` | [Qwen Code](https://github.com/QwenLM/qwen-code) | `qwen --acp` | `qwen` |
| `trae` | [Trae CLI](https://docs.trae.cn/cli) | `traecli acp serve` | `traecli` |
| `zeroclaw` | ZeroClaw | `zeroclaw acp` | `zeroclaw` |
| `devin` | [Devin](https://github.com/openclaw/acpx/blob/main/agents/Devin.md) | `devin acp` | `devin` |
| `fx` | [fx](https://github.com/openclaw/acpx/blob/main/agents/Fx.md) | `fx acp` | `fx` |
| `fast-agent` | [fast-agent](https://fast-agent.ai) | `uvx fast-agent-mcp acp` | `uvx` |
| `antigravity` | [Google Antigravity](https://github.com/openclaw/acpx/blob/main/agents/Antigravity.md) | `agy_acp_server.par` (`--uid=` on Linux, `.exe` on Windows) | the same binary |
| `openclaw` | [OpenClaw ACP bridge](https://github.com/openclaw/acpx/blob/main/agents/OpenClaw.md) | `openclaw acp` | `openclaw` |

Launch order: an `[acp.agents.<id>]` override wins, then an installed
adapter binary on `PATH`, then the zero-install `npx` / `uvx` form.
`agentos acp list` shows which one each id would use (`override`,
`installed`, `package-exec`) and which required CLIs are missing.

## Setup per harness

AgentOS never signs in for a harness. Install its CLI and log in once, as
the same OS user that runs the gateway; the harness keeps its own
credentials and billing.

- **Claude Code**: `npm install -g @anthropic-ai/claude-code`, then run
  `claude` once and log in (subscription OAuth), or export
  `ANTHROPIC_API_KEY`. Optional: `npm install -g
  @agentclientprotocol/claude-agent-acp` to skip the first-run download.
  Reports USD cost per turn.
- **Codex**: `npm install -g @openai/codex`, then `codex login` (ChatGPT
  plan) or export `OPENAI_API_KEY`. Optional: `npm install -g
  @agentclientprotocol/codex-acp`. With `isolate_codex_home = true`
  (default) the harness gets its own `CODEX_HOME` under
  `<state>/acp/codex-home/`: your `~/.codex/auth.json` is copied in, the
  session directory is marked trusted, and only `model`, `model_provider`,
  `model_reasoning_effort`, `sandbox_mode` and `[model_providers.*]` are
  inherited from your `~/.codex/config.toml`. Session modes:
  `read-only`, `workspace-write`, `full-access`. Codex reports tokens, not
  USD.
- **Gemini CLI**: `npm install -g @google/gemini-cli`, run `gemini` once to
  log in, or export `GEMINI_API_KEY`.
- **Cursor**: install the Cursor CLI and run `cursor-agent login`. If your
  install exposes ACP as `agent acp`, set an override (below).
- **GitHub Copilot**: `npm install -g @github/copilot`, then `copilot
  login`. Usage counts against your Copilot plan.
- **Everything else**: install the CLI from the link in the table, sign in
  with its own login command, then check with `agentos acp doctor <id>
  --probe`.

Credentials the harness reads from the environment (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, its OAuth files) pass through to it.
AgentOS's own secrets do not: every harness environment is scrubbed of
`AGENTOS_*`, gateway tokens, channel bot tokens and anything named
`*_TOKEN`, `*_SECRET` or `*_PASSWORD`. Add what a harness needs with
`[acp.agents.<id>] env`.

## Using it from a chat

The main agent delegates with `sessions_spawn`:

```json
{
  "task": "Fix the failing test in tests/test_api.py and run pytest.",
  "runtime": "acp",
  "acp_agent": "claude",
  "cwd": "/path/to/app",
  "mode": "oneshot"
}
```

| Parameter | Meaning |
| --- | --- |
| `runtime` | `"acp"` routes to a harness; the default `"subagent"` is an in-process AgentOS agent. |
| `acp_agent` | Harness id (default `[acp] default_agent`). Must be in `allowed_agents`. |
| `cwd` | Working directory (default: the agent workspace). Must be inside the workspace, a managed worktree or an `[acp] allowed_cwds` entry; see [Working directory](#working-directory). |
| `mode` | `oneshot` (default for spawn): the harness process exits after each turn, and a follow-up resumes the same harness session in a new process. `persistent`: the process stays up between turns. |
| `harness_mode` | ACP session mode id the harness advertises (`read-only`, `acceptEdits`, `plan`, ...). |
| `resume_session_id` | Carry a closed child's harness conversation (its `acp_session_id`) into a new child. A finished or stopped child is continued with `sessions_send` to its key instead; a session key passed here is refused with that advice. |
| `worktree`, `worktree_name`, `worktree_base_ref` | Run in a managed git worktree under `<state>/acp/worktrees/` instead of the checkout itself. |

The child session key looks like `agent:main:acp:1a2b3c4d`. After spawning,
the agent calls `sessions_yield`; the harness's final answer comes back to
the parent as a completion message with a stats line such as
`Stats: runtime 5m12s • tokens 48210 (in 45100 / out 3110) • cost $0.4120`.
The completion (the live `session.event.subagent_completion` event and the
parent's system message) also carries `runtime: "acp"`, `acp_agent`,
`acp_agent_name`, `cwd`, `mode`, `harness_mode` and a structured `usage`
object (`duration_ms`, `input_tokens`, `output_tokens`, `total_tokens`,
`cost_usd`, `tool_calls`; `null` when the harness did not report a value).
Other subagent completions carry `runtime: "subagent"`. The completion's
`result.text` is the harness's final answer: the text runs of the turn (split
by tool calls) from the last substantial one (at least 40 characters and half
the longest run) to the end, so interim "Now reading X." lines are dropped
and a report followed by a tool call and a closing remark is kept; the whole
text when no run qualifies. The child transcript keeps everything, with text
runs separated by a tool call as separate paragraphs. `result.text` holds
up to 12,000 characters and `result.full_chars` the answer's whole length;
when it was cut, the parent's wake says so ("The answer was cut at 12,000 of
17,116 characters; read the rest with sessions_history(session_key=...,
answer_offset=12000) - do not redo the work.") and
`sessions_history(session_key, answer_offset=N)` returns the rest of that same
answer in pages of up to 3,900 characters (sized so a page reaches the model
whole, now and in later turns) with a `next_offset`. Otherwise
`sessions_history` on an ACP child clips each tool input and result to 500
characters (`[…N chars]`) so the harness's own text is not lost to
truncation.

When the user stops a run (the card or bar Stop, `acp.cancel`, `acp close`,
the child chat's Stop), the completion carries `stopped_by: "user"`, a
`terminal_message` for the parent that its wake shows as well ("The user
stopped this Claude Code run after N tool calls (m:ss). Do not restart it;
tell the user what was done so far and ask how to continue.") and a
`user_message` for the chat ("You stopped this Claude Code run after N tool
calls (m:ss)."). When the parent stops its own child (`subagents kill`), the
completion says `stopped_by: "agent"` ("Stopped by the agent after ...") and
is recorded in the parent transcript without waking the parent, which
already answered in the turn that stopped it. A parent's `steer` replaces the
turn and is not reported as a stop. The stopped turn's `last_turn` carries
`stopped_by` and a `summary` ("Stopped by you after 3 tool calls (0:18).")
instead of an error text.

To continue a child, `sessions_send` to its key: `followup` queues the text
as the next turn, `steer` cancels the running turn and sends the new text.
This works for a finished `oneshot` child too: the harness starts again
with the recorded ACP session (`session/load`) and closes after the turn.
If the harness cannot resume it, a fresh harness session starts in the
same `cwd` and the turn's text begins with "Started a new `<harness>`
session; the previous context could not be resumed." The user can also
type into the child session directly; those turns are not announced to the
parent. Only a session closed by an operator (`acp.close`, `agentos acp
close`, `subagents kill`) refuses new turns; the refusal is logged
(`acp.dispatch.turn_refused`), written to the child transcript as an
`Error:` row for the person reading it ("This Claude Code session has
ended. Start a new chat to continue.") and sent as the normal `error` event,
whose `error_message` carries the agent-facing detail (spawn a new one,
`resume_session_id=...`). `subagents` lists ACP
children with `runtime: "acp"`, `acp_idle` (finished, takes follow-ups) and
`acp_closed`; `kill` cancels and closes one. `subagents` is a deferred tool:
spawning a harness loads it (otherwise one `tool_search` does). `acp.cancel` and
`acp.close` refuse a key that is not an existing ACP session.

The operator-only `acp` tool (not exposed by default) offers `agents`,
`sessions`, `status`, `cancel`, `close`, `set_mode`, `set_model` and
`doctor`.

## Working directory

With the default `[acp] allowed_cwds = []`, a harness may run in the agent
workspace and in managed worktrees (`worktree: true`), nowhere else.

- Read-only work (a review, a summary, an investigation) may run in the
  agent workspace when the spawn uses a read-only `harness_mode` (`plan`
  for Claude Code, `read-only` for Codex).
- Work that writes files belongs in the project's own checkout: add it to
  `allowed_cwds`, or spawn with `worktree: true` from a repository that is
  already allowed.
- A refused `cwd` fails the spawn with `ACP_CWD_NOT_ALLOWED`. The error
  tells the agent to check its guess first (a folder named in the task
  usually lives in the agent workspace) and, if the task really targets that
  directory, gives the exact command that allows it (the list is replaced as
  a whole, so keep the entries you already have):

```sh
agentos config set acp.allowed_cwds '["~/Code/my-app"]'
```

## Permission modes

Harnesses ask before they edit files or run commands. `[acp]
permission_mode` decides who answers:

| Mode | Behaviour |
| --- | --- |
| `ask` (default) | The prompt goes to the approval queue (namespace `acp`) and shows up in the Web UI / desktop / CLI approvals like any other approval. No answer within `approval_timeout_s` = rejected. |
| `approve-reads` | Reads, searches, fetches and thinking are allowed; anything else follows the `ask` path. |
| `approve-all` | Everything is allowed. Only this mode may answer "always allow". |
| `deny-all` | Everything is rejected; the harness continues without the tool. |

Answering an `ask` prompt with **Always allow** skips later prompts of the
same kind in that harness session. For commands the grant covers one
program and subcommand: Always allow on `git push origin main` covers
`git push --force` but not `git reset`. The program is matched by the
absolute path it resolves to on the gateway's `PATH`, so `/tmp/x/git push`
or `./git push` still asks. These commands always ask, even after Always
allow:

- anything with `;`, `&`, `|`, `<`, `>`, `$`, a backtick, `(`, `)`, `{`, `}`,
  `\` or a line break, quoted or not (chaining, pipes, redirection,
  substitution, expansion);
- a `VAR=value` prefix, a relative program path, or a program that does not
  resolve;
- shells and wrappers that run another command (`bash`, `sh`, `env`,
  `sudo`, `xargs`, `eval`, `nohup`, `timeout`, ...). `bash -lc '<command>'`
  is matched by the one command inside;
- a command that does not parse (an unbalanced quote).

With `ask` and nobody able to answer (cron, unattended runs),
`non_interactive_permissions = "fail"` fails the turn with
`ACP_PERMISSION_PROMPT_UNAVAILABLE`; `"deny"` rejects and lets the harness
carry on. Harness questions (ACP elicitation) go to the same queue under
`acp_elicitation`; they are declined after 15 minutes.

`agentos acp run` has no approval surface: it uses `approve-reads` with
reject-instead-of-ask, unless you pass `--permissions approve-all` or
`--permissions deny-all`. It declines harness questions.

Never give `approve-all` to a harness working on a branch or pull request
you have not read: a hostile repository can instruct the harness to run
anything.

## Sessions, resume and cancel

- Each harness session has a record in `<state>/acp/sessions/<record_id>.json`.
  A record is reused only when the harness id, working directory and
  launch command all match.
- Resume uses ACP `session/load` when the harness supports it, otherwise a
  new harness session starts. If resuming fails, AgentOS retries once
  fresh, then reports `ACP_SESSION_INIT_FAILED`.
- `persistent` sessions keep the harness process alive between turns until
  closed, cancelled or the gateway stops; `oneshot` sessions stop the
  process after every turn and stay resumable (`idle`) until an operator
  closes them.
- A turn that has to resume (a oneshot follow-up, a persistent session
  after a gateway restart) but gets a fresh harness session instead starts
  its text with a one-line notice that the previous context was lost.
- Cancel sends ACP `session/cancel`, waits 2 s, then stops the process
  group (SIGTERM, SIGKILL 750 ms later). A stopped turn keeps what it did
  in the child transcript (tool calls and any text) followed by a
  "Cancelled after N tool calls (m:ss)." row; its duration, tool calls and
  cost (when reported) still reach `last_turn` and the completion.
- Cost: Claude Code reports a running total per ACP session, and a resumed
  session may carry it over. AgentOS keys its baseline by ACP session id, so
  each turn is charged only its increase; a reading below the baseline (the
  total restarted) is charged as it is.
- One harness turn keeps up to 100,000 characters of text (the rest is
  marked as cut). While a turn runs, `activity` names the tool call in
  progress, "Thinking…" when none is running and no text has arrived, and
  "Writing the answer" while text streams.
- Tool rows in a child transcript are titled with a short label built from
  the tool call ("Run git status", "Read ~/app/src/x.py", "Search TODO",
  "Fetch docs.python.org", "Looking up tools"); the ACP tool kind is in the
  row's `input.acp_kind`.
- `turn_timeout_s` (0 = none) is a wall-clock limit per turn. A quiet
  harness is not a failed one; only the limit ends a turn.

```sh
agentos acp sessions                      # live from the gateway, else the stored records
agentos acp cancel agent:main:acp:1a2b3c4d
agentos acp close agent:main:acp:1a2b3c4d --discard   # also forget the resume record
agentos acp run claude "continue" --resume <acp-session-id>
```

Gateway RPC for UIs: `acp.sessions` lists sessions with a live harness
process; each row has `agent_name` (registry display name), `activity` (a
short label of the current or last step, such as `Read ~/app/src/x.py`,
`Run pytest -q` or `Writing the answer`), `activity_kind` (the ACP tool
kind `read|edit|delete|move|search|execute|think|fetch|other`, or `text`
while the answer streams), `turn_tool_calls` and, during a turn,
`turn_elapsed_s`. `acp.status {session_key}` returns the same fields for
any ACP session, live, idle or closed, plus `idle`, `closed`, `mode`,
`harness_mode`, `cwd` and `last_turn`; a non-ACP key fails with
`ACP_SESSION_INVALID`. `acp.cancel {session_key}` stops the running turn
(and any queued one); `acp.close {session_key, discard?}` closes the
session for good.

## Configuration

All keys live under `[acp]` (see also
[`configuration.md`](configuration.md#acp-coding-harnesses)):

```toml
[acp]
enabled = true                      # false = no ACP spawns, agentos acp run refuses
default_agent = "claude"
allowed_agents = ["*"]              # ids sessions_spawn(runtime="acp") may launch
permission_mode = "ask"             # ask | approve-all | approve-reads | deny-all
non_interactive_permissions = "fail"  # fail | deny (ask mode with nobody to answer)
approval_timeout_s = 900
turn_timeout_s = 0                  # 0 = no wall-clock limit
max_concurrent = 8                  # live harness sessions per gateway (1-64)
# state_dir = "~/.agentos/state/acp"
isolate_codex_home = true
install_missing_adapters = true     # allow npx -y / uvx downloads on first use
stream_delivery = "live"            # live (text streams as it arrives) | final_only
allowed_cwds = []                   # extra dirs a harness may use as cwd (besides workspace / worktrees)

[acp.agents.cursor]                 # per-harness override
command = "agent"
args = ["acp"]
env = { HTTPS_PROXY = "http://127.0.0.1:8080" }
```

`command` + `args` replace the registry launch for that id; an entry with
only `env` keeps the registry launch and adds variables. You can also add a
harness that is not built in by giving it a new id and a `command`.

State lives in `<state>/acp/` (default `~/.agentos/state/acp/`):
`sessions/`, `leases/`, `codex-home/`, `worktrees/`, and
`logs/<record_id>.stderr.log` (the harness's stderr, redacted, last 256 KiB).

## Troubleshooting

Start with `agentos acp doctor <id> --probe`; it shows the exact launch
command, missing CLIs, and whether ACP `initialize` succeeds.

- **The first turn hangs for a minute.** The `npx` / `uvx` launch downloads
  the adapter on first use. Install it once (`npm install -g
  @agentclientprotocol/claude-agent-acp`, `npm install -g
  @agentclientprotocol/codex-acp`) and `doctor` shows `installed`. With
  `install_missing_adapters = false` the download is refused and `doctor`
  says so.
- **`ACP_HARNESS_MISSING`.** The harness CLI is not on the gateway's `PATH`.
  Install it, or set `[acp.agents.<id>] command` to an absolute path. A
  gateway started by launchd or the desktop app may have a shorter `PATH`
  than your shell.
- **Claude Code says it is not logged in.** Do not add `--bare` to a Claude
  launch override: bare mode skips OAuth and the keychain, so subscription
  logins stop working. Run `claude` once interactively to refresh the login,
  or export `ANTHROPIC_API_KEY`.
- **Codex prints skill or frontmatter errors.** Codex loads skills from
  `~/.agents/skills`; a `SKILL.md` there with invalid YAML frontmatter makes
  it log errors (see `<state>/acp/logs/<record_id>.stderr.log`) and can fail
  the session. Fix or move that skill.
- **Codex asks to trust the directory.** Leave `isolate_codex_home = true`;
  the isolated home marks the session directory trusted. With it off, run
  `codex` once in that directory and trust it.
- **Harness processes left over after a crash.** Every harness gets a lease
  file in `<state>/acp/leases/` and an `AGENTOS_ACP_LEASE` marker in its
  environment. At gateway start, AgentOS stops leftover processes whose pid,
  command and marker match a stale lease, and never touches anything else.
- **`ACP_AGENT_NOT_ALLOWED`.** The id is not in `[acp] allowed_agents`, or
  `[acp] enabled = false`.
- **`ACP_CWD_NOT_ALLOWED`.** The spawn's `cwd` is outside the workspace,
  the managed worktrees and `[acp] allowed_cwds`. Run the `agentos config
  set acp.allowed_cwds ...` command from the error message.
- **`ACP_SESSION_CLOSED`.** The session was closed by an operator (`acp
  close`, `subagents kill`). Spawn a new one; `resume_session_id` (in the
  error) continues the harness conversation.
- **`ACP_UNAVAILABLE` from `agentos acp sessions/cancel/close`.** The
  running gateway has no ACP session manager (older build, or ACP disabled).
  `agentos acp list` / `doctor` still work locally.
- **`ACP_PERMISSION_PROMPT_UNAVAILABLE`.** `permission_mode = "ask"` in a run
  nobody can answer. Choose `approve-reads` / `deny-all` for that agent, or
  set `non_interactive_permissions = "deny"`.

Read next:

- [`sessions.md`](sessions.md)
- [`approvals-and-permissions.md`](approvals-and-permissions.md)
- [`cli.md`](cli.md#acp-coding-harnesses)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
