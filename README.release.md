# AgentOS

AgentOS is a local-first AI agent with native agentic trading, an on-device Pilot Router,
MCP-native tools, durable sessions, local memory, multi-channel messaging,
and a local web control UI.

Each release is published as the `use-agent-os` package on PyPI and as a
versioned wheel attached to the matching GitHub release, next to the Windows
portable zip (which bundles its own Python, the dependency wheelhouse, install
scripts, and third-party notices) and the macOS desktop app. Install a
published release rather than a source checkout, for example:

```sh
uv tool install --python 3.12 "use-agent-os[recommended]"
```

## Requirements

- Python 3.12 or newer (the Windows portable zip bundles its own).
- A configured model provider for live model calls.
- Optional channel credentials only for the channel integrations you enable.

## After Install

Run the onboarding command before starting the gateway:

```sh
agentos onboard --if-needed
```

Then start the local gateway:

```sh
agentos gateway run
```

## Project Links

Repository, license, release, and third-party notice information are included in
the release bundle and in the public AgentOS repository.
