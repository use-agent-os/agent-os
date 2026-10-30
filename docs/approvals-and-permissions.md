# Approvals and Permissions

Approvals and permissions control how AgentOS tools are allowed to act.
They matter most when an agent can write files, run shell commands, publish
artifacts, post into channels, or call external services.

Use this page before running unattended automation or giving a channel-connected
agent broad tool access.

## Permission Profiles

Single-shot automation accepts an explicit permission profile:

```sh
agentos agent --permissions restricted -m "Inspect this repo"
agentos agent --permissions on -m "Run with host execution and approvals"
agentos agent --permissions bypass -m "Trusted local automation"
agentos agent --permissions full -m "Fully trusted local automation"
```

Practical meaning:

| Profile | Use when |
| --- | --- |
| `restricted` / `off` | The task should stay conservative and avoid elevated execution. |
| `on` | Host execution is allowed, but approval checks still matter. |
| `bypass` | You trust the task enough to auto-grant approvals while keeping sensitive-path checks. |
| `full` | You fully trust the task and environment. Use sparingly. |

For automation, prefer the narrowest profile that can complete the task.

These profiles apply to interactive CLI and Web turns. For cron turns (unattended automation),
the default elevated execution posture is controlled by `permissions.cron_default_mode` (which
defaults to `bypass`). By default, cron jobs of kind `agent_turn` run elevated (under `bypass`),
whereas other job kinds (like reminders and script runs) are never elevated. You can explicitly
override this default for any individual job by passing `--no-elevated` (to opt out of elevation)
or `--elevated-mode {bypass,full}` (to pick a specific mode) during job creation or update.
See [`cli.md`](cli.md#letting-a-cron-job-run-shell-based-skills) for details and security
implications.

## Workspace Containment

Set a workspace for file and shell work:

```sh
agentos agent \
  --workspace /path/to/project \
  --workspace-strict \
  -m "Summarize this repo"
```

Contain writes to the workspace or scratch directory:

```sh
agentos agent \
  --workspace /path/to/project \
  --workspace-lockdown \
  --scratch-dir /path/to/project/.scratch \
  -m "Investigate and prepare a minimal patch"
```

Use `--workspace-lockdown` for unattended runs where accidental writes outside
the project would be unacceptable.

## Interactive Approvals

Interactive chat surfaces can pause sensitive tool calls for a human decision.
The terminal chat supports:

```text
/approvals
/approvals reset
/permissions status
/permissions on
/permissions off
/permissions bypass
/permissions full
/forget
```

Use these commands when you need to inspect or reset cached approval decisions
during a chat.

### Cached Intents

Approving a destructive action caches the *intent* — kind plus target path —
rather than the literal command, so a paraphrased retry (`rm /tmp/x` followed by
`os.remove("/tmp/x")`) proceeds without a second prompt.

The kind carries a set of *escalation capabilities*, and a cached approval
covers a retry only when its capability set is a superset of the retry's. The
capabilities are independent — they are not a single ladder:

| Capability | Meaning | Granted by |
|---|---|---|
| *(none)* | delete this one path | `rm X`, `os.remove`, `os.unlink`, `os.rmdir`, `Path(X).unlink()`, `Path(X).rmdir()` |
| `recursive` | delete the whole tree below it | `rm -r`/`-R`/`--recursive`, `shutil.rmtree(X)` |
| `parents` | may also delete empty *ancestors* | `os.removedirs(X)` |
| `force` | `rm`'s `-f`/`--force` | `rm -f`/`--force` |

So a `delete:recursive` approval covers a plain `rm X`, but a `delete:force`
approval does **not** cover `shutil.rmtree(X)` — `force` says nothing about
recursion. Escalation always re-prompts: approving `rm /tmp/logs` does **not**
authorize `rm -rf /tmp/logs`.

`/approvals` lists cached entries as `scope kind:target`, and `/forget <path>`
drops every grade recorded for that path.

The Web UI also provides an approvals surface for reviewing pending actions
outside the message scrollback.

### Image Flow Spend Prompts

An agent's `flow_run` (docs/flows.md) asks before it spends more than
`[flows].agent_cost_limit_usd` on one run, or an amount that cannot be
estimated, in its own namespace, `flow`. Every surface offers only **Allow
once** and **Deny**: no "Always Allow", no "Bypass" / "Stop Asking", and the
gateway ignores both for this namespace if a client sends them, so a spend
approval never unlocks further spend or elevates the session. A session-wide
bypass (`/elevated`, or "Stop Asking" on a shell prompt) releases pending
`exec` prompts only, never a spend. A denial sticks: for 30 minutes every
later `flow_run` of that flow in that session asks again, whatever its inputs
or cost.

### Coding-Harness (ACP) Prompts

Coding harnesses started with `sessions_spawn(runtime="acp")` (Claude Code,
Codex, Gemini CLI, ...) ask for permission through the same approval queue,
in two extra namespaces:

| Namespace | What it is | Approve means | Deny / timeout means |
| --- | --- | --- | --- |
| `acp` | The harness wants to run a command, edit a file, fetch a URL, ... Shown as `"<harness> wants to <kind>: <title>"`, for example `Codex wants to run: git push`. | Allow this one call. | Reject this call; the harness carries on without it. |
| `acp_elicitation` | The harness asks a question (a small form) or asks you to finish a step in the browser (a link). Shown as `"<harness> asks: <message>"` or `"<harness> asks you to open a link: <url>"`. | Answer: forms send their values as the resolve payload `{"content": {"<field>": <value>}}`; a link means "I finished the step". | Decline. |

How a harness prompt is answered depends on `[acp].permission_mode`:

| `permission_mode` | Behaviour |
| --- | --- |
| `ask` (default) | Every prompt waits in the approval queue for up to `[acp].approval_timeout_s` (default 900 s); no answer rejects it. |
| `approve-reads` | Reads, searches, fetches and thinking are allowed without asking; anything else is asked as above. |
| `approve-all` | Everything is allowed without asking. |
| `deny-all` | Everything is rejected without asking. |

A spawn may pick a stricter mode than the configured one, never a looser one.
When nobody can answer (an unattended cron or channel run), `ask` follows
`[acp].non_interactive_permissions`: `fail` (default) ends the harness turn
with `ACP_PERMISSION_PROMPT_UNAVAILABLE`, `deny` rejects the prompt and lets
the harness continue. Questions are always declined when nobody can answer.

Choosing **Always** (or **Bypass**) on an `acp` prompt remembers the grant for
that harness session only, per tool kind and, for commands, per program and
subcommand (`git push`): it never changes the session's elevated mode and is
forgotten when the harness session closes. Forms are asked at most three
questions at a time, a secret field always on its own; answers are cleared from
the queue as soon as the harness has them and are never logged. Questions time
out after 15 minutes.

Over RPC, `approval.list` lists pending approvals of every namespace with a
readable `title` (`params.namespace` narrows it), and `exec.approval.resolve`
(or `POST /api/approvals/resolve`) accepts an optional `payload` object for
form answers.

### Where a prompt reaches you

Every pending approval is listed on the console's approvals page and in the
desktop app's approval dialog. Beyond that:

- **Web Push.** In the web console, Settings → *Push notifications for
  approvals* subscribes this browser (it asks for notification permission;
  it needs HTTPS or `localhost`). Each new approval is pushed with a link to
  the approvals page; when it is resolved a replacement notice says how —
  granted, denied, expired, or cancelled because its run was stopped. The
  push goes out from the same queue write that announces `approval.resolved`
  to consoles. Messages are encrypted
  for the subscribed browser, expire with the approval and carry only its
  title. Turn it off per gateway with `[push] web_enabled = false`.
- **Parent sessions.** An approval records `audience_session_keys`: the
  session that asked, then the sessions that spawned it (a coding harness's
  or subagent's parent, and that session's parent). The `approval.requested`
  event and `GET /api/approvals` carry it, and the chat view of any of
  those sessions shows the turn as *waiting for approval* while the child's
  prompt is pending. When the nearest such parent is delivered on Telegram,
  Discord or Slack, the prompt with *Approve* / *Deny* buttons is also sent to
  that chat.
- **Channel custody.** On Telegram, Discord and Slack, *Approve* / *Deny*
  works only in the chat the approval's session is delivered to — a DM
  session answers to its own sender, a group or channel session to members of
  that chat, a child session to its parent's chat, and the shared main DM
  session to the chat it last delivered to. A paired sender cannot resolve a
  prompt that belongs to the web console, the desktop or another chat.

## Sandbox Posture

Inspect sandbox posture:

```sh
agentos sandbox status
agentos sandbox status --json
```

Set posture:

```sh
agentos sandbox on
agentos sandbox bypass
agentos sandbox full
agentos sandbox reset
```

Restart the gateway after changing global sandbox posture:

```sh
agentos gateway restart
```

## Recommended Defaults

| Situation | Recommended approach |
| --- | --- |
| First run in a repo | `--workspace` plus `--workspace-strict` |
| Read-only investigation | `--permissions restricted` |
| Local patch with tests | `--workspace-lockdown` plus a scratch directory |
| Web UI task with writes | Keep approvals visible and review sensitive actions |
| Channel-connected agent | Conservative permissions and explicit channel setup |
| Unattended automation | Bound timeout/iterations and choose the narrowest workable permissions |

## Troubleshooting

If a tool is denied:

```sh
agentos sandbox status
agentos doctor
```

Then check:

- whether the surface supports live approvals;
- whether the workspace path is correct;
- whether cached approvals need to be reset;
- whether the task should run with a different permission profile.

Read next:

- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`web-ui.md`](web-ui.md)
- [`channels.md`](channels.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
