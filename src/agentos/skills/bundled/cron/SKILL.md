---
name: cron
description: "Use when the user asks to schedule recurring tasks, one-off reminders, timers, or cron-style jobs through the AgentOS cron tool."
always: false
triggers:
  - schedule
  - recurring
  - timer
  - cron
  - every
  - reminder
  - remind
  - 提醒
  - 每分钟
  - 每5分钟
  - 每天
  - 定时
provenance:
  origin: openclaw-derived
  license: MIT
  upstream_url: https://github.com/openclaw/openclaw
  maintained_by: AgentOS
metadata:
  agentos:
    requires_tools:
      - cron
---

# Cron Skill

When the user asks to schedule something, set up a recurring task, create a timer, or create a reminder, use the `cron` tool.

The `schedule` argument is a **structured object**, not a string. Choose one shape and translate any natural language yourself before calling the tool — the tool will not parse free-form text and will reject flat strings with a structured error.

Three accepted schedule shapes:

- `{"kind": "cron", "expr": "<5-field POSIX cron>", "tz": "<optional IANA timezone>"}`
  Recurring on a calendar pattern. Example: `{"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Shanghai"}` for weekdays at 09:00 Shanghai wall time.
- `{"kind": "every", "every_seconds": <integer ≥ 1>}`
  Recurring on a fixed sub-minute or odd interval. Example: `{"kind": "every", "every_seconds": 30}` for every 30 seconds.
- `{"kind": "at", "at": "<ISO-8601 with timezone>"}`
  One-shot at an absolute time. The timestamp must include a timezone offset.

Translation examples (do this in your own reasoning before calling the tool):

- "每5分钟提醒我喝水" → `cron(action="add", schedule={"kind": "cron", "expr": "*/5 * * * *"}, task="喝水", job_kind="system_event", session_target="main")`
- "每30秒打印一次" → `cron(action="add", schedule={"kind": "every", "every_seconds": 30}, task="...", job_kind="agent_turn", session_target="isolated")`
- "明天早上9点叫我" → compute the absolute ISO-8601 string with timezone, then `cron(action="add", schedule={"kind": "at", "at": "<that ISO-8601>"}, task="...", job_kind="system_event", session_target="main")`
- "every weekday at 9am Los Angeles time" → `cron(action="add", schedule={"kind": "cron", "expr": "0 9 * * 1-5", "tz": "America/Los_Angeles"}, task="...")`

## Running a script instead of a model

`job_kind="script"` runs a file on schedule and delivers its stdout — no LLM
call, no tokens. The `script` must be a path relative to `~/.agentos/scripts/`;
`script_args` is a list passed as argv (never through a shell). Empty stdout
means the run stays silent, and a non-zero exit is delivered as a failure.

```
cron(action="add", schedule={"kind": "every", "every_seconds": 300},
     job_kind="script", script="watch-memory.sh")
```

`job_kind="agent_turn"` with a `script` is the other half: the script runs
first, its stdout becomes context for the turn, and a tick where it prints
nothing skips the turn entirely.

```
cron(action="add", schedule={"kind": "cron", "expr": "*/10 * * * *"},
     job_kind="agent_turn", script="watch_github.py",
     script_args=["--repo", "owner/name"],
     task="Summarize anything urgent.")
```

Either way, scheduling a script needs an interactive CLI or Web caller — it is
refused from a chat channel. The bundled `cron-watchers` skill ships scripts for
RSS feeds, JSON endpoints, and GitHub repos that already follow this contract.

## Where the job announces

By default a job reports back to the conversation it was created in, which is
what "remind me" means. Pass `delivery` only when the user names a different
destination:

```
cron(action="add", schedule={"kind": "cron", "expr": "0 9 * * 1-5"},
     job_kind="agent_turn", task="Summarize yesterday's incidents.",
     delivery={"mode": "channel", "channel_name": "telegram",
               "channel_id": "-1001234567890"})
```

- `mode="channel"` needs `channel_name`: the name of a channel that is actually
  configured. That is usually the adapter type (`telegram`, `slack`,
  `discord`), but an operator may have named the entry something else — where a
  channel manager is reachable, an unconfigured name is rejected and the error
  lists the real ones. `channel_id` is the id the *provider* uses — a Telegram
  numeric chat id, negative for a group, or `@username`. It is never an AgentOS
  session key like `agent:main:telegram:direct:1245463966`; the tool rejects
  those with the id you probably meant. Leave `channel_id` empty to use the
  channel's configured default chat. `thread_id` is optional — a Slack
  thread ts, a Telegram forum topic id or a Discord thread id — and
  `account_id` is stored but not yet honoured by channel delivery.
- `mode="none"` schedules a job that announces nowhere.
- `mode="origin"` (or omitting `delivery`) keeps the calling conversation.
- `best_effort: true` keeps a failed delivery from failing the run. It applies
  to any mode.

The destination fields only mean something under `mode="channel"`, so pairing
one with `mode="origin"` or `mode="none"` is an error rather than a silent
fallback to the calling chat — if you name a channel, say `mode="channel"`.
Webhook delivery and failure destinations are not available here; they are
configured from the CLI, the Web UI, or the cron RPC.

Choosing a channel requires an interactive CLI or Web caller and a
`session_target` other than `main` — from a chat channel the job always
delivers back to that same conversation. Do not invent a chat id: if the user
has not given one, ask, or leave it empty for the channel default.

The `add` response echoes the resolved `delivery`, so confirm the destination
from that rather than assuming it.

## Elevation and Security

By default, scheduled `agent_turn` jobs run elevated under the `bypass` mode (controlled globally by `permissions.cron_default_mode`), allowing them to execute shell commands. Other job kinds (like reminders and script runs) are never elevated. You can explicitly override this default for any individual job by passing `"elevated": "off"` (or another elevated mode like `"full"`) inside `tool_policy`.

## Editing an existing job

Never re-create a job to change it. `cron(action="add", ...)` builds a fresh job
from defaults, so a "replace it" strategy silently resets what you did not pass:
an `agent_turn` becomes a `reminder`, a job pinned to a timezone moves to UTC,
its tool policy disappears, and its output starts landing in the current chat
instead of the channel it was reporting to.

- Read first: `cron(action="get", job_id="<job id>")` returns every setting —
  `job_kind`, `tz`, schedule, `session_target`, delivery target, `tool_policy`,
  `wake_mode`, `timeout_seconds`, and the script fields. `action="list"` is a
  summary and does not show them.
- Change in place: `cron(action="update", job_id="<job id>", task="<new prompt>")`
  patches only what you pass and keeps the job's id, schedule, timezone,
  delivery, kind, and policy. The same call accepts `schedule`, `tz`, `name`,
  `job_kind`, `session_target`, `tool_policy`, `wake_mode`, `delivery`, and
  `enabled`.
- Move where it announces: pass `delivery` to `update`. The job keeps its id and
  its run history, which remove-and-re-create throws away.

```
cron(action="update", job_id="<job id>", schedule={"kind": "cron", "expr": "30 7 * * 1-5"})
cron(action="update", job_id="<job id>", enabled=False)
cron(action="update", job_id="<job id>",
     delivery={"mode": "channel", "channel_name": "telegram",
               "channel_id": "-1001234567890"})
```

## Cloning a job

"Make another one like that, but …" is `clone_from`, not add-then-remove. The
clone inherits the source's timezone, kind, session target, delivery target,
tool policy, wake mode, script, and schedule; anything passed alongside
overrides that one field. The source keeps running.

```
cron(action="add", clone_from="<job id>", task="Summarize yesterday's incidents")
cron(action="add", clone_from="<job id>", schedule={"kind": "cron", "expr": "0 18 * * *"})
```

Cloning a one-shot `{"kind": "at"}` job needs an explicit `schedule` — the
source's fire time is already spent. A clone always starts enabled, even when
the source is paused.

A job that carries a script or an elevated tool policy can only be cloned or
updated by an interactive CLI or Web caller, and so can a job that reports to a
channel or webhook the current session cannot post to — editing its text or
cloning it would be authoring content for somebody else's destination.

`name` sets the display name on both `add` and `update`, so a job can keep a
short readable name while its prompt is long.

Passing `delivery` alongside `clone_from` redirects the clone: the explicit
destination wins over the one the source carried. To move the *existing* job
instead of making a second one, pass `delivery` to `action="update"` — the same
operator rules apply, and `mode="channel"` still needs an interactive CLI or Web
caller.

Other actions:

- List: `cron(action="list")`.
- Inspect: `cron(action="get", job_id="<job id>")`.
- Trigger now: `cron(action="run", job_id="<job id>")`.
- Cancel: `cron(action="remove", job_id="<job id>")`.

Cron expression format: `minute hour day month weekday` (e.g. `0 9 * * 1-5` = weekdays at 9am).
