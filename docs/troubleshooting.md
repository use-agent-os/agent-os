# Troubleshooting

Stuff broke? Don't panic. Start here:

```sh
agentos doctor
agentos doctor --json
agentos gateway status
```

The Web UI health view at <http://127.0.0.1:18791/control/> also tells you
what's going on when the gateway is running.

---

## `agentos` Command Not Found

After installing with `uv tool install`, your shell doesn't know about the new
binary yet. Either open a fresh terminal or refresh your PATH:

```sh
uv tool update-shell
```

Verify it's there:

```sh
command -v agentos
```

On Windows PowerShell:

```powershell
where.exe agentos
```

If that still doesn't work, make sure `uv` itself is installed and on your
PATH. Run `uv --version` to check.

---

## Gateway Is Not Running

The gateway needs to be up for most things to work. Start it:

```sh
agentos gateway run
```

Or kick it off in the background:

```sh
agentos gateway start --json
agentos gateway status
```

Then open <http://127.0.0.1:18791/control/> in your browser.

If it crashes on startup, check the logs:

```sh
agentos gateway status --json
```

A common reason: the config file has a typo or a provider isn't set up yet.
Run `agentos doctor` — it usually points you right at the problem.

For a deeper look at gateway lifecycle and config, see [`gateway.md`](gateway.md).

---

## Port Already In Use

Something else is already listening on the default port. Either kill the
other process, or just use a different port:

```sh
agentos gateway run --port 18792
```

If you had a managed gateway running, stop it first:

```sh
agentos gateway stop
```

---

## Provider Not Configured

No provider set up means no model to talk to. Run onboarding:

```sh
agentos onboard
```

Or configure one manually:

```sh
agentos providers list
agentos providers configure openrouter
```

For secrets, always use environment variables instead of hardcoding keys:

```sh
export OPENAI_API_KEY="sk-..."
agentos configure provider --provider openai --api-key-env OPENAI_API_KEY
```

If you're getting auth errors even after setting the key, double-check the
variable name and that you exported it in the same shell session where you
start the gateway. `.env` files are loaded at process start — changing one
after the gateway is already running won't help until you restart.

---

## Router Dependency Problems

Pilot Router is optional. If it can't load, AgentOS falls back to direct
model routing — it still works, just without the cost optimization.

If you want to disable it explicitly:

```sh
agentos configure router --router disabled
agentos gateway restart
```

### Windows: DLL Load Failed

On Windows, Pilot Router needs the Visual C++ Redistributable (Visual Studio
2015-2022 x64). The portable installer and the PowerShell source installer
handle this for you via `winget`, but the `uv tool install` path does not.

If you see a `DLL load failed` error in the logs:

1. Download and install the
   [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).
2. Restart your terminal.
3. Restart the gateway.

### Router Shows "Degraded" or Pins Everything to One Tier

This usually means the ONNX model bundle is missing or incomplete. If you
installed from source, make sure you pulled the LFS files:

```sh
git lfs pull --include="src/agentos/agentos_router/models/**"
```

Then rebuild and reinstall. If you're on a release install, this shouldn't
happen — but a fresh `agentos upgrade` never hurts.

You can also switch to the `llm_judge` strategy, which needs no local model
files at all:

```sh
agentos configure router --strategy llm_judge
agentos gateway restart
```

---

## Search Does Not Work

Search is a separate capability from the LLM provider. Check what's set up:

```sh
agentos search list
agentos search status
```

### Quick fix: use DuckDuckGo (no API key needed)

```sh
agentos configure search --search-provider duckduckgo
agentos gateway restart
```

### With Brave Search

```sh
export BRAVE_SEARCH_API_KEY="..."
agentos configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
agentos gateway restart
```

If search still fails, check your network. Some corporate proxies and VPNs
block the search backend endpoints. Try `curl` against the search API directly
to rule out network issues.

---

## Browser Tool Not Found or Not Working

The browser tool is hidden by default until the `agent-browser` binary is
installed. If the agent isn't offering browser actions, install it:

```sh
npm install -g agent-browser
agent-browser install          # downloads Chromium
```

On Debian, Ubuntu, or Docker you'll also need system libraries:

```sh
agent-browser install --with-deps
```

Run `agentos doctor` — it tells you whether the binary and Chromium are
present.

### Headless Chromium Gets Blocked

Some sites detect headless browsers and throw up CAPTCHAs or flat-out refuse
to load. This is normal — headless Chromium is fingerprintable.

The agent should not try to solve CAPTCHAs. If a site blocks you, switch to
`web_search` / `web_fetch` instead, or use
[attach mode](features/browser.md#attach-consent) with your own signed-in
Chrome.

### Attach Mode Won't Connect

Attach mode requires your Chrome to be running with a debug port on
localhost:

```sh
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

Then set both the port and the consent flag in your config:

```toml
[browser]
cdp_port = 9222
attach_confirmed = true
```

The port alone isn't enough — you must also set `attach_confirmed = true`.
This is on purpose, because attach mode can drive whatever your Chrome session
is logged into.

---

## Channel Config Saved but Channel Is Offline

Saving a channel config writes it to disk, but the running gateway needs a
restart to pick it up:

```sh
agentos gateway restart
agentos channels status <name> --json
```

For webhook-based channels (like Slack Events API), also make sure:

- The gateway is reachable from the internet (or from Slack's servers).
- The callback URL in your Slack app config points to the right port.
- The signing secret matches what you configured in AgentOS.

For Telegram, if the bot is online but messages aren't getting through, check
that pairing is set up — see [`channels.md`](channels.md#telegram-account-pairing).

---

## A Tool Was Denied

AgentOS has a layered sandbox. If a tool call gets rejected, check what's
going on:

```sh
agentos sandbox status
agentos doctor
```

For one-shot runs, you can set an explicit permission level:

```sh
agentos agent --permissions restricted -m "Read only"
agentos agent --permissions full -m "Trusted local automation"
```

If you keep hitting denials on something you trust, the workspace containment
might be too strict. Check your workspace flags and sandbox posture in the
Web UI under Agent Setup.

---

## The Agent Seems to Forget Old Context

This is usually compaction at work. When a session gets long, AgentOS
compresses older context to stay within the model's context window. It's
expected behavior, not a bug.

See what happened:

```sh
agentos sessions show <session-key>
agentos sessions export <session-key>
```

If exact old text matters, keep it in a file, a memory note, or an exported
transcript. The agent can always reference those when needed.

To reduce how aggressively things get compacted, you can lower the
compression threshold in your config — but that means higher token costs per
turn.

---

## A Turn Is Too Expensive or Too Slow

A few things to try:

```sh
agentos configure router --router recommended
agentos diagnostics on
agentos cost
```

Diagnostics will show you which tier each turn landed on and why. If the
router keeps picking expensive models for simple tasks, check your tier
configuration.

For bounded automation runs, set limits:

```sh
agentos agent --max-iterations 20 --timeout 600 -m "Bounded task"
```

Large tool outputs are a common culprit. Tool compression helps:

- [`features/tool-compression.md`](features/tool-compression.md)
- [`features/compaction-and-cache.md`](features/compaction-and-cache.md)

---

## Memory or Embeddings Not Working

Memory search uses local ONNX embeddings by default. If recall feels broken:

```sh
agentos memory status
```

If the embedding model isn't loaded, you might be missing the `recommended`
install extra. Reinstall with:

```sh
uv tool install --force "use-agent-os[recommended]"
```

For source installs, make sure the model weights are actually present (they
come via Git LFS):

```sh
git lfs pull --include="src/agentos/memory/models/**"
```

If the model files are pointer stubs instead of real weights, embeddings won't
load. `agentos doctor` catches this.

---

## Docker-Specific Issues

If you're running AgentOS in a container, a few things behave differently:

- The gateway can't bind to `127.0.0.1` inside Docker — use `--listen 0.0.0.0`
  and map the port with `-p`.
- ONNX Runtime and Pilot Router may need extra system packages depending on
  your base image. The Dockerfile in the repo handles this, but a custom
  image might not.
- Volume mounts for `~/.agentos` are needed if you want config and sessions
  to persist across container restarts.

---

## Still Stuck?

If nothing above helped:

1. Run `agentos doctor` and read the output carefully — it's usually pretty
   good about pointing at the real problem.
2. Check the [docs index](README.md) for the feature you're trying to use.
3. Open a
   [documentation issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
   or a [bug report](https://github.com/use-agent-os/agent-os/issues/new?template=bug_report.yml)
   on GitHub.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
