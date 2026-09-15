# Scheduling

AgentOS scheduling lets you run recurring or one-time agent work from the
gateway. Use it for reminders, periodic summaries, status checks, channel
updates, and webhook-delivered automation.

Scheduling is managed with the `agentos cron` command group.

## Requirements

Scheduled jobs run through the gateway:

```sh
agentos gateway run
```

For long-lived local use, start the managed gateway:

```sh
agentos gateway start --json
agentos gateway status
```

## List Jobs

```sh
agentos cron list
agentos cron list --agent main
agentos cron list --json
```

Job visibility is scoped to the selected profile, not to the session that
created each job. Calls from the Control UI, CLI, or a paired channel therefore
see the same profile-wide schedule list. The creation session is retained as
delivery metadata and shown as **Created from** when available.

## Add an Interval Job

Run a prompt every hour:

```sh
agentos cron add \
  --every 1h \
  --text "Summarize important project updates" \
  --name hourly-project-check
```

Intervals accept values such as `30s`, `5m`, and `1h`.

## Add a Cron Expression

Run on weekdays at 09:00 in a named timezone:

```sh
agentos cron add \
  --cron "0 9 * * 1-5" \
  --tz "America/Los_Angeles" \
  --text "Prepare a short morning brief" \
  --name weekday-morning-brief
```

Use `--exact` when you do not want the default stagger.

Day-of-month and day-of-week follow the POSIX rule: when **both** fields are
restricted (neither is a bare `*`), the job fires when *either* matches. So
`0 0 1,15 * 5` runs on the 1st, the 15th, **or** any Friday. When one of the two
is `*`, the fields are ANDed as usual — `0 0 1 * *` is the 1st only, and
`0 0 * * 5` is Fridays only.

## Add a One-Time Job

```sh
agentos cron add \
  --at "2026-06-01T09:00:00+00:00" \
  --text "Remind me to review the launch checklist" \
  --name launch-checklist-reminder
```

## Job Kinds

`--job-kind` decides what actually happens when a job fires. It defaults to
`auto`, which resolves to `reminder` for normal targets, `system_event` for
`--session-target main`, and `script` when you pass `--script`.

| Kind | What fires | Spends tokens |
| --- | --- | --- |
| `reminder` | Your `--text` is delivered verbatim. Nothing else runs. | No |
| `script` | A file in `~/.agentos/scripts/` runs; its stdout is delivered. | No |
| `agent_turn` | The agent runs `--text` as a prompt and its reply is delivered. | Yes |
| `system_event` | The text is written into the main session and wakes the heartbeat. | Yes |

An `agent_turn` job can also carry a `--script`, which then runs *before* the
turn as a data collector — see [Pre-run scripts](#pre-run-scripts).

The default matters: `agentos cron add --every 1h --text "Summarize updates"`
creates a **reminder**, so it repeats that sentence every hour rather than
summarizing anything. Add `--job-kind agent_turn` when you want the agent to do
the work.

## Run a Script Instead of a Model

A `script` job is the watchdog shape — poll something on a timer, deliver a line
when it matters, stay quiet otherwise — with no LLM in the loop:

```sh
mkdir -p ~/.agentos/scripts
cat > ~/.agentos/scripts/watch-memory.sh <<'EOF'
#!/usr/bin/env bash
used=$(ps -A -o %mem | awk '{s+=$1} END {printf "%d", s}')
[ "$used" -gt 90 ] && echo "⚠ memory at ${used}%"
EOF

agentos cron add --every 5m --script watch-memory.sh --name memory-watchdog
agentos cron update <job-id> --script watch-disk.sh
```

The script path is **relative to `~/.agentos/scripts/`**. Absolute paths, `~`,
and `..` are refused, and a symlink that leaves the directory is refused at run
time — the directory is the trust boundary. `.sh` and `.bash` run under bash;
every other extension runs under the same Python interpreter as the gateway.
Pass `--workdir` to run somewhere other than the script's own directory. A
relative `--workdir` is joined onto that same directory, so `--workdir data`
names `~/.agentos/scripts/data`; an absolute one is taken as given. A workdir
that does not exist is reported in the log and the script runs in its own
directory.

Subdirectories under `~/.agentos/scripts/` are allowed, and `{job_id}` anywhere
in the path is replaced with the created job's own id:

```sh
agentos cron add --every 10m --script 'lp-monitor/{job_id}/tick.sh' --name lp-ratchet
```

That gives a job a directory named after itself in a single `add` — the id does
not exist until the job does, so the alternative is to create the job against a
staging path and repoint it afterwards, leaving a live job pointing at a path it
will not keep. The job it prints back carries the resolved path in its `script`
field; write the file there. A job whose stored path somehow still holds
`{job_id}` refuses to run rather than creating a directory by that name.

Arguments go through `--script-arg`, repeated once per argument:

```sh
agentos cron add --every 15m --script watch_rss.py --name hn-watch \
  --script-arg --name --script-arg hn \
  --script-arg --url --script-arg https://news.ycombinator.com/rss
```

They are passed to the script as argv with no shell in between, so a value
containing spaces or `;` stays one argument and cannot start a second command.
The Web UI takes them as one line and splits it the way a shell would.

The bundled **cron-watchers** skill ships ready-made scripts for the common
sources (RSS/Atom, a JSON endpoint, a GitHub repo) that already follow this
contract — ask the agent for it, or see
`agentos skills view cron-watchers`.

What the job does with the result:

- **stdout** → delivered verbatim, exactly as printed (capped at 16k characters).
- **no stdout** → silent run. Nothing is delivered and the run counts as a
  success, so a watchdog that prints only on trouble stays quiet.
- **a final line of `{"wakeAgent": false}`** → also treated as silence, so
  watchdog scripts written for other runtimes work unchanged.
- **non-zero exit or timeout** → the error is delivered *and* the job fails, so
  a broken watchdog cannot be mistaken for a quiet one. `--timeout` bounds the
  run (default 600s).

Secrets are masked in both stdout and stderr before delivery, and AgentOS's own
controls (`AGENTOS_GATEWAY_TOKEN` and the redaction/sensitive-path switches) are
withheld from the child process. Provider credentials are *not* — a script
inherits `OPENAI_API_KEY` and friends exactly like every other AgentOS child
process, which is what lets a watcher call a model API of its own. Set
`AGENTOS_STRIP_PROVIDER_ENV=1` to withhold those too.

**What you are accepting.** The script runs on this host as you, on schedule,
with nobody watching and no approval prompt — there is no LLM deciding what to
run, but also nothing reviewing it. Treat `~/.agentos/scripts/` as trusted as
your shell profile, and remember that anything with write access to that
directory can schedule itself. For that reason a script job can only be created
by an interactive CLI or Web caller: the in-agent `cron` tool refuses
`job_kind='script'` from a channel, which keeps a chat message from scheduling
unattended execution.

Script jobs never take `--elevated` — elevation only means something for a job
that runs an agent turn.

## Pre-run Scripts

The other half of the same idea: keep the script, but let an agent read what it
found instead of the user. Add `--script` to an `agent_turn` job and it runs
first, as a data collector:

```sh
agentos cron add --every 10m --name repo-triage \
  --job-kind agent_turn \
  --script watch_github.py \
  --script-arg --repo --script-arg owner/name \
  --script-arg --scope --script-arg issues \
  --text "Summarize anything here that looks urgent. Stay brief."
```

Per tick:

- **stdout** → prepended to the prompt as `## Script output`, then the turn runs
  with your `--text` after it.
- **no stdout** (or a `{"wakeAgent": false}` final line) → the turn is skipped
  entirely. No LLM call, no session, no transcript line, no delivery. This is
  what makes the pattern cheap: the agent only wakes on ticks with news.
- **non-zero exit** → the error is prepended as `## Script error` and the turn
  runs anyway, so the agent can tell the user the collector broke.

Same directory rule, same argv handling, and the same operator gate as a script
job. The script's stdout is untrusted input arriving inside a prompt — the
header says so to the model, but the turn still runs elevated by default
(`cron_default_mode="bypass"`), so that output can drive real shell-based skill
calls with no approval prompt. Pass `--no-elevated` to drop a job back to a
read-only tool surface, and prefer that whenever a pre-run script reads the
open internet.

## Choose the Session Target

The default target is an isolated session. For most scheduled work, that is the
least surprising option.

Useful targets:

| Target | Use when |
| --- | --- |
| `isolated` | Each scheduled run should stand alone. |
| `session` | You want to deliver into a specific session configured by the runtime surface. |
| `current` | The job should continue in the session that created it (requires a bound session). |
| `main` | You want a system event for the main session. |

Example:

```sh
agentos cron add \
  --every 30m \
  --session-target isolated \
  --text "Check for urgent channel updates" \
  --name urgent-update-check
```

## Delivery

Disable delivery:

```sh
agentos cron add \
  --every 1h \
  --text "Create a private summary" \
  --no-deliver \
  --name private-hourly-summary
```

Deliver through a webhook:

```sh
agentos cron add \
  --every 1h \
  --text "Post a compact status summary" \
  --webhook-url https://example.com/hooks/agentos \
  --webhook-token-env AGENTOS_WEBHOOK_TOKEN \
  --name webhook-status-summary
```

Prefer `--webhook-token-env` or `--webhook-token-file` over inline tokens so
secrets do not land in shell history.

### Naming a channel recipient

Channel delivery is configured from the Web UI, the RPC API, or the in-agent
`cron` tool (below), and its
**Recipient** is the id the *provider* uses — a Telegram chat id
(`1245463966`, negative for a group) or `@username`, a Slack channel id. It is
not an AgentOS session key: `agent:main:telegram:direct:1245463966` names a
conversation inside AgentOS, and Telegram answers `chat not found` for it.

Both are visible in the UI and the two are easy to confuse, so a save that
carries a session key is rejected outright, and the gateway asks the channel
whether the chat exists before storing it. Where the recipients are known —
Telegram, whose pairing store lists every chat the bot may talk to — the Web UI
offers them as a dropdown, with an `Enter manually…` escape hatch for a group
chat that is not in `group_chat_ids`.

### Asking the agent for a specific destination

The in-agent `cron` tool takes an optional `delivery` object, so a job created
in conversation can announce somewhere other than the chat it was asked for:

```json
{
  "mode": "channel",
  "channel_name": "telegram",
  "channel_id": "-1001234567890",
  "best_effort": false
}
```

`delivery` is accepted on `add` and on `update`, so an existing job can be moved
without being re-created. `mode` is `origin` (the calling conversation —
identical to omitting `delivery` on `add`), `channel`, or `none`. `channel_id` follows the same recipient
rules as above and is validated when the job is saved, so a session key or a
non-numeric Telegram target fails immediately rather than at the next fire; an
empty `channel_id` uses the channel's configured default chat. `channel_name`
is checked against the configured channels, so a typo cannot be saved — note
that it is the entry's *name*, which is the adapter type unless the operator
renamed it. A destination field paired with `mode='origin'` or `mode='none'` is
rejected rather than quietly falling back to the caller. `best_effort` applies
to any mode.

Unlike the Web UI, the tool does not probe the adapter to confirm the chat
exists, and `account_id` is accepted and binds the job to a specific account
instance in multi-account setups. Webhook delivery and failure destinations
are refused outright here —
they stay CLI/Web/RPC-only, so an agent cannot be talked into POSTing job
output at an arbitrary URL.

`mode='channel'` needs an interactive CLI or Web caller and a `sessionTarget`
other than `main`. Asked from a chat channel it is refused and the job keeps
delivering to that same conversation, so a participant in one room cannot aim
scheduled output at another. Note that this gate is the same strength as the
one on `--elevated` and script jobs: `callerKind` says the request entered
through the gateway, not that a human approved it. Inside a Web or CLI session
the agent can set it without a confirmation step, so untrusted content the
agent reads can in principle steer the destination — the `delivery` block
echoed in the `add` response is there to be checked.

## Inspect and Run Jobs

```sh
agentos cron status <job-id>
agentos cron runs <job-id>
agentos cron runs <job-id> --limit 50
```

Run a job immediately:

```sh
agentos cron run <job-id> --yes
```

`cron runs` shows each run's `Output` (the reply, or a script job's stdout) and
`Delivery` (where that output went). Use `--json` for untruncated output.

### Asking the agent what a job did

The in-agent `cron` tool reads the same history through `action="runs"`, so you
can ask in chat — "what did the memory watchdog report today?" — and the model
answers from the recorded runs instead of guessing what the schedule produced:

```json
{"action": "runs", "job_id": "<job-id>", "limit": 5}
```

It returns each run's `started_at`, `success`, `output`, and `delivery`, plus
`error` on a failure. `limit` defaults to 5 and is clamped to 20, and output
longer than 2000 characters comes back with `output_truncated: true` — the full
text is always available from `agentos cron runs <job-id> --json`. The action is
scoped to the caller's own profile, the same boundary `remove` and `run` use.

A script job's stdout can contain whatever the script fetched (an RSS item, an
API response), and `action="runs"` puts that text in the model's context. That
is the same content a delivered job already mirrors into the chat, but a job
that reports nowhere is now readable too, so treat script output as untrusted
input the same way you would any fetched content.

## Update or Remove Jobs

```sh
agentos cron update <job-id> --enabled
agentos cron update <job-id> --disabled
agentos cron update <job-id> --every 2h
agentos cron remove <job-id> --yes
```

Primary delivery destinations are not patched in place from the CLI. Remove and
re-add a job when the primary channel or webhook destination needs to change.

### Editing and cloning a job from chat

The in-agent `cron` tool edits jobs the same way, so "change that reminder to
7:30" does not have to become a delete-and-recreate — which would reset the
job's kind, timezone, tool policy, and delivery target to defaults:

```json
{"action": "get",    "job_id": "<job-id>"}
{"action": "update", "job_id": "<job-id>", "task": "Summarize yesterday's PRs"}
{"action": "update", "job_id": "<job-id>", "schedule": {"kind": "cron", "expr": "30 7 * * 1-5"}}
{"action": "update", "job_id": "<job-id>", "enabled": false}
```

`action="get"` returns the full record — kind, timezone, schedule, session
target, delivery, tool policy, wake mode, timeout, script fields — which
`action="list"` does not. `action="update"` patches only the keys it is given
and keeps the job's id and everything else.

Deriving a second job from an existing one is `clone_from` on `add`. The clone
inherits every setting of the source and overrides only what is passed
alongside; the source keeps running:

```json
{"action": "add", "clone_from": "<job-id>", "task": "Summarize yesterday's incidents"}
```

Delivery is one of the settings a clone inherits, so a clone announces wherever
its source did unless it is given a `delivery` of its own — passing one
redirects the clone and leaves the source alone. To move the existing job rather
than derive a second one, pass `delivery` to `update`:

```json
{"action": "update", "job_id": "<job-id>",
 "delivery": {"mode": "channel", "channel_name": "telegram", "channel_id": "-1001234567890"}}
```

A repoint keeps the job's id, its run history, and its websocket topic, so
subscribers watching the job stay attached. The gates are the same ones `add`
applies: `mode='channel'` needs an interactive CLI or Web caller and a session
target other than `main`, the recipient is validated at save time, and a chat
caller cannot repoint a job that already answers somewhere that chat cannot
address. Webhook destinations and failure destinations remain CLI/Web/RPC-only.

Both paths keep the operator gates. A job carrying a script or
`tool_policy.elevated` can only be cloned or updated by an interactive CLI or
Web caller. So can a job whose delivery points somewhere the calling session
cannot post — another channel, or a webhook — because rewriting its text or
cloning it would put the caller's words on somebody else's destination. A
webhook's URL and token are never disclosed by `action="get"`: it reports the
host plus `webhook_url_set` / `webhook_token_set`. Cloning a one-shot `at` job
requires an explicit new schedule, and a clone always starts enabled.

## Troubleshooting

Check the gateway and job state:

```sh
agentos gateway status
agentos cron list
agentos cron status <job-id>
agentos cron runs <job-id>
```

If a job posts to a channel, also check:

```sh
agentos channels status
```

A `script` job that appears to do nothing is usually working as designed: empty
stdout means silence. `agentos cron runs <job-id>` distinguishes the two — a
silent run is recorded with a `silent: script produced no output` summary, while
a broken script is recorded as a failure with the exit code and stderr.

Read next:

- [`channels.md`](channels.md)
- [`operations.md`](operations.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
