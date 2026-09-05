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
