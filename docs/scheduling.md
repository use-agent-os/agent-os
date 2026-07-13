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

## Add a One-Time Job

```sh
agentos cron add \
  --at "2026-06-01T09:00:00+00:00" \
  --text "Remind me to review the launch checklist" \
  --name launch-checklist-reminder
```

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

## Update or Remove Jobs

```sh
agentos cron update <job-id> --enabled
agentos cron update <job-id> --disabled
agentos cron update <job-id> --every 2h
agentos cron remove <job-id> --yes
```

Primary delivery destinations are not patched in place from the CLI. Remove and
re-add a job when the primary channel or webhook destination needs to change.

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

Read next:

- [`channels.md`](channels.md)
- [`operations.md`](operations.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
