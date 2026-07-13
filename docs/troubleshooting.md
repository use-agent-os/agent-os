# Troubleshooting

Start with:

```sh
agentos doctor
agentos doctor --json
agentos gateway status
```

The Web UI health view at <http://127.0.0.1:18791/control/> also reports
readiness and recovery steps when the gateway is running.

## `agentos` Command Not Found

After `uv tool install`, open a new terminal or run:

```sh
uv tool update-shell
```

Check the executable:

```sh
command -v agentos
```

On Windows PowerShell:

```powershell
where.exe agentos
```

## Gateway Is Not Running

Start it:

```sh
agentos gateway run
```

Or use the managed background process:

```sh
agentos gateway start --json
agentos gateway status
```

Open:

```text
http://127.0.0.1:18791/control/
```

For a focused gateway guide, see [`gateway.md`](gateway.md).

## Port Already In Use

Use another port:

```sh
agentos gateway run --port 18792
```

Or stop the managed gateway:

```sh
agentos gateway stop
```

## Provider Not Configured

Run:

```sh
agentos onboard
agentos providers list
agentos providers configure openrouter
```

Use environment-variable secrets:

```sh
export OPENAI_API_KEY="sk-..."
agentos configure provider --provider openai --api-key-env OPENAI_API_KEY
```

## Router Dependency Problems

If AgentOS Router cannot load, AgentOS can still run with direct model
routing. To disable the router:

```sh
agentos configure router --router disabled
agentos gateway restart
```

On Windows, ONNX Runtime may need the Visual C++ Redistributable for Visual
Studio 2015-2022 x64. Install it, then restart the shell and gateway.

## Search Does Not Work

Inspect search providers:

```sh
agentos search list
agentos search status
```

Use DuckDuckGo for a no-key path:

```sh
agentos configure search --search-provider duckduckgo
```

Use Brave with a key:

```sh
export BRAVE_SEARCH_API_KEY="..."
agentos configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
```

## Channel Config Saved but Channel Is Offline

Restart the gateway after editing channel config:

```sh
agentos gateway restart
agentos channels status <name> --json
```

For webhook channels, confirm the gateway is reachable from the provider and
that callback secrets match.

## A Tool Was Denied

Check sandbox and permission state:

```sh
agentos sandbox status
agentos doctor
```

For one-shot runs, choose an explicit permission posture:

```sh
agentos agent --permissions restricted -m "Read only"
agentos agent --permissions full -m "Trusted local automation"
```

## The Agent Seems to Forget Old Context

Long sessions may compact old history. This is expected under context pressure.

Inspect sessions:

```sh
agentos sessions show <session-key>
agentos sessions export <session-key>
```

If exact old text matters, keep it in a file, memory note, or exported session.

## A Turn Is Too Expensive or Too Slow

Try:

```sh
agentos configure router --router recommended
agentos diagnostics on
agentos cost
```

For automation:

```sh
agentos agent --max-iterations 20 --timeout 600 -m "Bounded task"
```

For large tool outputs, see
[`features/tool-compression.md`](features/tool-compression.md).

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
