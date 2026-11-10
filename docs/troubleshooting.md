# Troubleshooting

Start with:

```sh
agentos doctor
agentos doctor --json
agentos gateway status
```

The Web UI's Settings › Health at
<http://127.0.0.1:18791/control/settings/health> also reports readiness and
recovery steps when the gateway is running.

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

If Pilot Router cannot load, AgentOS can still run with direct model
routing. To disable the router:

```sh
agentos configure router --router disabled
agentos gateway restart
```

On Windows, ONNX Runtime may need the Visual C++ Redistributable for Visual
Studio 2015-2022 x64. The portable installer and the PowerShell source
installer install it via `winget`; the `uv tool install` path does not.

If logs show `DLL load failed`:

1. Install the
   [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
   (2015-2022 x64).
2. Restart the shell and gateway.

### Router Degraded or Pinned to One Tier

A missing or incomplete ONNX model bundle is the usual cause. From a source
checkout:

```sh
git lfs pull --include="src/agentos/agentos_router/models/**"
```

Then rebuild and reinstall. Release installs ship the bundle in the wheel.

To use the `llm_judge` strategy instead (no local model files), pick it during
onboarding or set:

```toml
[agentos_router]
strategy = "llm_judge"
```

Restart the gateway after changing the file. See
[`configuration.md`](configuration.md#router-strategy).

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

## Browser Tool Not Found or Not Working

The browser tool stays hidden until the `agent-browser` binary is installed:

```sh
npm install -g agent-browser
agent-browser install          # downloads Chromium
```

On Debian, Ubuntu, or Docker, also install system libraries:

```sh
agent-browser install --with-deps
```

`agentos doctor` reports whether the binary and Chromium are present.

A gateway started by the desktop app, launchd or systemd does not inherit your
shell's `PATH`, so AgentOS also looks in the usual npm global directories: the
newest nvm and fnm Node versions, Volta, pnpm, Bun, Homebrew (`/opt/homebrew/bin`,
`/usr/local/bin`) and `~/.npm-global/bin`. If your install lives somewhere else,
set the path explicitly:

```toml
[browser]
binary_path = "/path/to/agent-browser"   # output of `which agent-browser`
```

### Headless Chromium Gets Blocked

Some sites detect headless Chromium and serve CAPTCHAs or refuse to load.
Do not try to solve CAPTCHAs. Switch to `web_search` / `web_fetch`, or use
[attach mode](features/browser.md#attach-consent) with a signed-in Chrome.

### Attach Mode Will Not Connect

Start Chrome with a debug port on localhost:

```sh
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

Then set both the port and the consent flag:

```toml
[browser]
cdp_port = 9222
attach_confirmed = true
```

The port alone is not enough. `attach_confirmed = true` is required because
attach mode can drive whatever the Chrome session is logged into.

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

## Memory or Embeddings Not Working

Memory search uses local ONNX embeddings by default. Inspect:

```sh
agentos memory status
```

If the embedding model is not loaded, the `recommended` extra may be missing:

```sh
uv tool install --force "use-agent-os[recommended]"
```

For source installs, pull the Git LFS weights:

```sh
git lfs pull --include="src/agentos/memory/models/**"
```

If the model files are pointer stubs instead of real weights, embeddings will
not load. `agentos doctor` will report memory embeddings as FTS-only.

## Docker-Specific Issues

The shipped `Dockerfile` already sets `AGENTOS_LISTEN=0.0.0.0`. Custom images
must bind `0.0.0.0` (`--listen 0.0.0.0` or `AGENTOS_LISTEN=0.0.0.0`) so the
port mapping reaches the gateway. Binding `127.0.0.1` inside the container is
valid, but the process is then unreachable from the host.

- A wildcard bind needs enforced auth: without it the gateway logs
  `Refusing to serve: auth.mode='none'`, exits `78`, and a restart policy loops
  it. The shipped `compose.yaml` requires `AGENTOS_AUTH_TOKEN` for this reason.
- ONNX Runtime and Pilot Router may need extra system packages depending on
  the base image. The repo Dockerfile handles this; a custom image may not.
- State lives under `AGENTOS_STATE_DIR`, `/var/lib/agentos` in the shipped image.
  Mount a volume there if config and sessions should persist across restarts.

More symptoms and fixes are in
[Run the gateway in a container](container-gateway.md#troubleshooting).

## Still Stuck?

1. Run `agentos doctor` and read the findings.
2. Check the [docs index](README.md) for the feature you are using.
3. Open a
   [documentation issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
   or a [bug report](https://github.com/use-agent-os/agent-os/issues/new?template=bug_report.yml)
   on GitHub.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
