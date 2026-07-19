# Quickstart

This guide gets AgentOS installed, configured, and running locally. It
assumes you want the standard product experience: terminal commands, local Web
UI, Pilot Router, memory/search support, and safe local defaults.

## Requirements

- Python 3.12 or newer for terminal installs.
- `uv` for the recommended terminal install.
- Git and Git LFS only when installing from source.
- A provider API key unless you use a local provider such as Ollama.

## Recommended Install

Install the current release (recommended extras included) by following the
[Installation](../README.md#installation) section of the README. The newest
release assets are always on the
[Releases page](https://github.com/use-agent-os/agent-os/releases/latest).

The `recommended` extra includes Pilot Router dependencies and memory/search
support used by the default product experience.

If `agentos` is not found after install, open a new shell or run:

```sh
uv tool update-shell
```

## First-Run Setup

Interactive setup:

```sh
agentos onboard
```

Script-friendly setup:

```sh
export OPENROUTER_API_KEY="sk-..."
agentos onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

Useful variants:

```sh
agentos onboard --if-needed
agentos onboard --minimal
agentos onboard --provider openai --api-key-env OPENAI_API_KEY
agentos onboard --provider ollama --model llama3.1
```

`--if-needed` is safe for install scripts because it avoids rewriting an
already-ready setup. `--minimal` configures the provider path and skips optional
channels/search/image-generation sections.

Check onboarding state:

```sh
agentos onboard status
```

## Run the Gateway

Foreground gateway:

```sh
agentos gateway run
```

Background gateway with readiness wait:

```sh
agentos gateway start --json
agentos gateway status
```

Default address:

```text
http://127.0.0.1:18791/control/
```

The gateway defaults to loopback for safety. To bind elsewhere, opt in:

```sh
agentos gateway run --listen 0.0.0.0 --port 18791
```

Only expose a non-loopback gateway behind appropriate auth and network controls.

## First Useful Run

Open the Web UI:

```text
http://127.0.0.1:18791/control/
```

Start terminal chat:

```sh
agentos chat
```

Run one automation turn:

```sh
agentos agent -m "Inspect this workspace and suggest a test plan"
```

Run a one-shot task in a specific workspace:

```sh
agentos agent \
  --workspace /path/to/project \
  --workspace-strict \
  -m "Review the current diff and list the highest-risk changes"
```

Use the Web UI for browser-based chat, approvals, setup, channels, usage, and
logs. Use `agentos chat` when you want a terminal conversation. Use
`agentos agent` for one-shot automation.

## Resume Work

Resume a terminal chat session:

```sh
agentos chat --session <session-key>
```

Inspect sessions:

```sh
agentos sessions list
agentos sessions show <session-key>
agentos sessions export <session-key>
```

Export a session when exact history matters for debugging or handoff.

## Check Readiness

Run these after setup:

```sh
agentos doctor
agentos providers list
agentos search list
agentos channels types --json
```

If the gateway is running, inspect runtime status:

```sh
agentos gateway status
agentos providers status
agentos channels status
agentos memory status
```

For provider/model selection details, see
[`providers-and-models.md`](providers-and-models.md). For search setup, see
[`search.md`](search.md).

For gateway lifecycle, host/port, and exposure guidance, see
[`gateway.md`](gateway.md).

## Stop or Restart

Foreground gateway:

```text
Ctrl+C
```

Managed background gateway:

```sh
agentos gateway stop
agentos gateway restart
```

## Upgrade

Upgrade a `uv tool` install to the latest release, then restart the
gateway so it runs the new code:

```sh
uv tool upgrade use-agent-os
agentos gateway restart
```

The upgrade keeps the extras from the original install, and your
configuration and data in `~/.agentos/` are not touched. To check
the installed version, run `uv tool list`. For source installs, see
the [README upgrade section](../README.md#upgrade).

## Next Steps

After the first run:

1. Configure search if you want web research:
   [`search.md`](search.md).
2. Enable channels if you want Slack, Telegram, Discord, or another
   messaging surface: [`channels.md`](channels.md).
3. Review memory behavior if you want durable recall:
   [`features/memory.md`](features/memory.md).
4. Review tool permissions before unattended automation:
   [`tools-and-sandbox.md`](tools-and-sandbox.md).
5. Learn Pilot Router if you want cost-aware model routing:
   [`features/agentos-router.md`](features/agentos-router.md).
6. Use the glossary if product terms are unfamiliar:
   [`glossary.md`](glossary.md).

## Install From Source

Use source install when you want a checkout-backed install:

```sh
git lfs install
git clone https://github.com/use-agent-os/agent-os.git
cd agent-os
git lfs pull --include="src/agentos/memory/models/**"
bash scripts/install_source.sh
```

For development, use the repository virtual environment:

```sh
uv sync --extra recommended --extra dev
uv run agentos --help
uv run agentos gateway run
```

When developing from source, prefix commands with `uv run` so they use the
checkout you are editing.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
