# Installation and Upgrade

The full install reference: every install path, installer options, upgrade
flags, and advanced gateway setups. The [README](../README.md#installation)
has the short version.

AgentOS works on Windows, macOS, and Linux. Pick the option that
fits what you need.

Windows portable and Quick terminal install give you a ready-made
**release**. You do not need Git for these. The other two — Install
from source and Develop from source — need a **Git checkout**
(`git clone` + Git LFS).

Release installs use files from GitHub releases. The Windows
portable zip also has a short link at `/releases/latest/download/`
for the newest release. Python wheel installs use file names with
version numbers, because the installer checks the version in the
file name.

| Path | Audience | When to use |
| --- | --- | --- |
| [AgentOS for Mac](#agentos-for-mac) | macOS users | A signed desktop app that installs the engine for you and updates itself |
| [Windows portable](#windows-portable-no-python) | Windows users | No Python needed; just unzip and run |
| [Quick terminal install](#quick-terminal-install) **(recommended)** | End users on any OS | Install a release from a terminal |
| [Install from source](#install-from-source) | Users who want the latest `main` code | Run from a Git checkout, but don't edit it |
| [Develop from source](#develop-from-source) | Contributors | Edit, test, or debug the code |

## Prerequisites

| Requirement | Windows portable | Quick terminal install | Install from source | Develop from source |
| --- | :---: | :---: | :---: | :---: |
| Python 3.12+ | bundled | via `uv` | via `uv` or system | via `uv` |
| Git + Git LFS | — | — | required | required |
| `uv` | — | installed if missing | recommended | required |

The default `recommended` profile installs **Pilot Router** — AgentOS's
own on-device model router (strategy `pilot-v1`) — along with the Python
dependencies it needs (ONNX Runtime, NumPy, Tokenizers). Its model
bundle ships inside the wheel, so routing works offline with no extra
download. If you install from a source checkout, run
`git lfs pull` first; without it the bundle is only pointer stubs, and the
router degrades gracefully — it logs a warning at boot and pins every turn to
the default tier (c1) rather than crashing. The `llm_judge` strategy needs no
local model files at all (it routes via a small LLM call, optionally against a
local Ollama/LM Studio endpoint) — pick it during onboarding or set
`agentos_router.strategy = "llm_judge"`.
Set `AGENTOS_INSTALL_PROFILE=core` to skip the router dependencies, or use
the `--router disabled` flag during first-time setup to keep them installed
but turn the router off.

On Windows, Pilot Router needs the Visual C++ runtime too (it comes
with the ONNX engine inside Pilot Router). The Windows portable
launcher and the from-source PowerShell installer install this
runtime for you, using `winget`. The **Quick terminal install**
(`uv tool install`) does not do this step. If you see a
`DLL load failed` error in the logs, install it by hand (see
[Troubleshooting](#troubleshooting)). AgentOS still works without
it — it just routes every turn to one single model instead.

Install links: [Git](https://git-scm.com/downloads) ·
[Git LFS](https://git-lfs.com/) ·
[uv](https://docs.astral.sh/uv/getting-started/installation/).

## AgentOS for Mac

The desktop app for macOS 13 or newer, Apple silicon and Intel. It is
signed with a Developer ID and notarized, so it opens without any
Gatekeeper workaround.

1. Download the dmg for your Mac from the
   [latest release](https://github.com/use-agent-os/agent-os/releases/latest)
   (or pick a version from the
   [releases page](https://github.com/use-agent-os/agent-os/releases)):
   `AgentOS-<version>-arm64.dmg` for Apple silicon, `AgentOS-<version>.dmg`
   for Intel. The number in the file name is the app's build version; the
   release it belongs to is the tag.
2. Drag AgentOS into Applications and open it. On first launch it installs
   the engine (the same `use-agent-os` package the terminal install uses)
   and starts the gateway for you.
3. Later releases show up inside the app as an **Update** pill; nothing
   downloads or installs until you click it. See
   [`desktop/README.md`](../desktop/README.md) for how the app and the engine
   update together.

## Windows portable (no Python)

This is the fastest way to start on Windows. The zip file has its
own copy of Python inside, so you don't need to install Python
yourself.

1. Download the current portable zip:
   <https://github.com/use-agent-os/agent-os/releases/latest/download/AgentOS-windows-x64-portable.zip>
2. Unzip it into a folder you can write to, like Downloads or
   Documents. Then right-click `Start AgentOS.cmd` and choose
   **Run as administrator**.
3. Finish the first-time setup. Then open
   <http://127.0.0.1:18791/control/> in your browser.

> [!NOTE]
> Preview builds are not signed, so you need to run them as
> administrator. If Windows SmartScreen shows a warning, click
> **More info**, then **Run anyway**. If your device blocks unsigned
> apps completely, use [Quick terminal install](#quick-terminal-install)
> instead.

<details>
<summary>Advanced portable usage</summary>

You can set an OpenRouter key before the first start:

```powershell
$env:OPENROUTER_API_KEY="sk-..."
Set-ExecutionPolicy -Scope Process Bypass
.\start.ps1
```

If `OPENROUTER_API_KEY` is set and there is no config file yet, the
launcher writes a config that points to this key. Then it starts the
gateway right away, with no questions asked. If the key is not set,
the setup wizard asks you to pick a provider.

The portable zip does not add a global `agentos` command to your
system. If you want a terminal where `agentos …` works, run
`AgentOS Shell.cmd`. Or call the launcher directly, like this:

```powershell
.\agentos.cmd onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

</details>

## Quick terminal install

This is the best path for Windows, macOS, and Linux. `uv` installs
AgentOS in its own space and manages its own Python. You do not need
Python on your system already. This path only installs official
releases. If you want `main`, other branches, or a local checkout,
use [Install from source](#install-from-source) instead.

**1. Install `uv`.** Skip this if `uv --version` already works for
you.

Linux / macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows PowerShell:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
```

**2. Install AgentOS.** This command is the same on every system.

```sh
uv tool install --python 3.12 "use-agent-os[recommended]"
```

This installs the latest AgentOS release from PyPI. Then `uv`
downloads whatever else that install needs. The default
`recommended` extra brings in local memory embedding's tools, like ONNX
Runtime, NumPy, and tokenizers. So the first install needs internet
access, unless these files are already saved on your computer.

**3. Set up and run.**

```sh
agentos onboard
agentos gateway run
```

> [!NOTE]
> If `agentos` is not found right after installing with `uv`, open a
> new terminal window. Or run the PATH command from step 1 again.

For an install pinned to one exact version, add `==<version>` to the
package name, or install the versioned wheel from that GitHub release. The
[README](../README.md#quick-terminal-install) always shows the command and
wheel link for the current release.

> [!NOTE]
> Release install commands use published GitHub release assets.
> Python wheel installs use versioned wheel filenames, because the
> installers validate the version segment inside the wheel filename, so
> there is no `latest` wheel alias. Only the Windows portable zip has a
> version-independent `releases/latest/download/` alias.

## Upgrade

The primary path is the built-in command, which detects how AgentOS was
installed, upgrades it, and — crucially — restarts the managed gateway so it
runs the new code (a "successful" upgrade that leaves the daemon on old code is
the #1 documented upgrade regret):

```sh
agentos upgrade
```

On a `uv tool` or `pipx` install this first snapshots your config and state
databases into `~/.agentos/state/snapshots/`, then installs the published
release of `use-agent-os[recommended]` for you (`uv tool install --force
--python <running> …`). The release comes from PyPI, or from the GitHub
release wheel when GitHub is ahead or PyPI cannot be reached. It then restarts
the gateway and **verifies** the running gateway reports the new version before
printing `Gateway: restarted and verified (<version>)`, and checks the state
databases. It prints the `old → new` versions on success.

`agentos upgrade` always installs the release, never a local checkout — the PyPI
wheel ships a control UI built in CI, while reinstalling a checkout would
re-package whatever browser bundle happens to be on disk. If you installed from
source, run `bash scripts/install_source.sh` again instead; it rebuilds the
control UI first. The command says so when it detects a checkout-backed install.

Flags:

| Flag | Effect |
|---|---|
| `--check` | Report whether a newer release exists on PyPI or GitHub Releases; change nothing. Offline → `could not check (offline)`, exit 0. |
| `--dry-run` | Print the exact upgrade command that would run; touch nothing. |
| `--source <auto\|pypi\|github>` | Where to install from. `auto` (default) prefers PyPI and falls back to the GitHub release wheel. |
| `--no-snapshot` | Skip the pre-upgrade snapshot of config and state databases (taken by default; the newest three are kept). |
| `--no-restart` | Upgrade the package but leave the gateway running the OLD code. Prints an unmissable warning; you must then run `agentos gateway restart` yourself. |
| `--timeout <s>` | Upgrade-subprocess timeout (default 600s). On timeout the tool's process group is killed and recovery guidance is printed — never a half-state. |
| `--verify-data` | Only check the state databases (`PRAGMA quick_check`); change nothing. |
| `--restore-snapshot <dir\|latest>` | Put a pre-upgrade snapshot back (the gateway must be stopped) and exit. |
| `--json` | Machine-readable output. |

See [`docs/cli.md`](cli.md#upgrade) for the full upgrade reference.

For a plain-`pip`, Windows portable, editable, or unclassifiable install,
`agentos upgrade` will **not** fake it — it prints the exact manual command and
exits non-zero. Run it yourself (the portable venv has no pip, so its command
restores it with `python -m ensurepip` first):

```sh
# pip install
python -m pip install --user --upgrade "use-agent-os[recommended]"

# install from source
cd agent-os
git pull
git lfs pull --include="src/agentos/memory/models/**"
git lfs pull --include="src/agentos/agentos_router/models/**"
bash scripts/install_source.sh        # Windows: scripts/install_source.ps1
```

**Version skew.** Commands that talk to the gateway compare the CLI and gateway
versions once per run. A gateway running an *older* version than the CLI (the
normal state right after a package upgrade, before a restart) prints a warning
but never blocks. A gateway running a *newer* version than the CLI (you
downgraded the CLI, or are driving a newer gateway from a stale environment) is
**refused** — a newer gateway may have written config with a newer schema, so
acting on it from an older CLI risks corruption. Fix it by upgrading the CLI or
restarting the gateway from this environment; in an emergency, set
`AGENTOS_ALLOW_VERSION_SKEW=1` to override.

**Update notices.** On gateway-connected commands the CLI checks PyPI at most
once every 24h and, if a newer release exists, prints a one-line notice on
stderr. It is suppressed on non-interactive / CI runs, and you can turn it off
entirely with `updates.notify = false` (see
[docs/configuration.md](configuration.md#update-notifications) or the
setup UI's Finish step).

**Config & data are safe.** Your configuration and data in `~/.agentos/` are
not touched by upgrades. Config migrations run at gateway start and write a
timestamped backup before rewriting any file. A version-pinned install
(`==<version>`) stays on its pin — to move it, re-run the install command with
the new version. To check the installed version, run `agentos gateway status`
(shows both the CLI and running-gateway versions) or `uv tool list`.

## Install from source

Use this path to run AgentOS from a Git checkout, without changing
the code. The checkout is just the source the installer reads from.
After install, use the `agentos` command — do not use `uv run`. If
you plan to change the code, use
[Develop from source](#develop-from-source) instead.

1. **Clone the code, with the large files too**

   Source installs build the bundled Control UI and therefore require
   **Node.js 22 or newer** in addition to Python 3.12+, Git, Git LFS, and `uv`.

   ```sh
   git lfs install
   git clone https://github.com/use-agent-os/agent-os.git
   cd agent-os
   git lfs pull --include="src/agentos/memory/models/**"
   git lfs pull --include="src/agentos/agentos_router/models/**"
   ```

2. **Run the installer**

   **macOS / Linux**

   ```sh
   bash scripts/install_source.sh
   ```

   **Windows PowerShell**

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1
   ```

   This script first builds and verifies the React Control UI, then installs
   `.[recommended]` (Pilot Router + memory + local models). It uses `uv tool
   install`, in its own environment. If
   `uv` is not there, it falls back to `python -m pip install
   --user`. If `agentos` is not found after install, open a new
   terminal.

3. **(optional) Install extra features.** Telegram, Slack, Discord, and
   Email already work with the base install. The remaining opt-in extra is
   `document-extras`, which adds PDF generation through WeasyPrint.

   ```sh
   AGENTOS_INSTALL_EXTRAS=document-extras bash scripts/install_source.sh        # macOS / Linux
   ```

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1 -Extras document-extras   # Windows
   ```

4. **Set up and run** — see [Configuration](../README.md#configuration).

<details>
<summary>Install from source — terminal prerequisites and installer options</summary>

**Install Git, Git LFS, Node.js 22+, and uv from a terminal**

Windows PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id GitHub.GitLFS -e
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
git lfs install
```

macOS (Homebrew):

```sh
brew install git git-lfs uv
git lfs install
```

Debian / Ubuntu:

```sh
sudo apt update && sudo apt install -y git git-lfs
curl -LsSf https://astral.sh/uv/install.sh | sh
git lfs install
```

On Fedora, use `sudo dnf install -y git git-lfs`. On Arch, use
`sudo pacman -S --needed git git-lfs`. Then install `uv` with the
`curl` command above. After running these installers, PATH changes
only apply in new terminal windows.

**Installer environment variables and PATH checks**

```sh
AGENTOS_INSTALL_PROFILE=core   bash scripts/install_source.sh   # small install, no Pilot Router
AGENTOS_INSTALL_DRY_RUN=1      bash scripts/install_source.sh   # just show the plan, don't install
```

To check which `agentos` your shell is using, run `command -v
agentos` (macOS/Linux) or `where.exe agentos` (Windows). If it is
not on your PATH, run `uv tool update-shell`. If you reinstall from
a local checkout, restart the gateway so it uses the new files.

</details>

## Develop from source

Use this path when you want to change AgentOS's code: writing
changes, running tests, or checking behavior in this checkout. This
is not the normal way to install AgentOS. Unlike
[Install from source](#install-from-source), this path needs `uv`:
`uv sync` builds a `.venv` folder inside the repo, and `uv run` runs
commands using the files in this checkout.

```sh
uv sync --extra recommended --extra dev
python scripts/build_control_ui.py build
uv run agentos --help
```

The `recommended` extra also brings Pilot Router here. The `dev` extra
adds the test, lint, and type-check tools. The build command uses Node.js 22+
to produce and verify the React Control UI served by the local gateway. To add
more extras to
this same setup:

```sh
uv sync --extra recommended --extra dev --extra document-extras
uv run agentos channels types --json
```

In this mode, add `uv run` before every `agentos` command shown in
[Configuration](../README.md#configuration). Do not test a development checkout
through a normal, user-installed `agentos` command — that command
uses a different Python setup.


## Advanced configuration

**Connect and check a messaging channel**

Saving a channel's settings does not mean it is actually connected
and working. After you change channel settings, restart the
gateway. Then check the real, live status:

```sh
agentos gateway restart
agentos channels status <name> --json
```

Only treat a channel as connected when the status shows
`enabled=true`, `configured=true`, and `connected=true`. Discord
uses websocket mode by default, Telegram uses polling, Slack can
use Socket Mode, and Email polls an IMAP inbox — none of these need a
public URL. Telegram webhook
mode and Slack webhook mode do need a public URL that the provider
can reach.

**Public network binding**

To open the Web UI from another computer, bind the gateway to all
network interfaces, and use this computer's public IP address:

```sh
agentos gateway run --listen 0.0.0.0 --port 18791
```

To let other computers in, you also need to open this port in your
firewall or cloud security settings. Do not run the gateway with
`[auth] mode = "none"` while doing this — set up token login first,
before you bind to `0.0.0.0`.

**Docker**

A prebuilt multi-arch image (linux/amd64 and linux/arm64) is published
for every release. Point Compose at it and set a gateway token, which it
requires, and nothing has to be built:

```sh
cat > .env <<EOF
AGENTOS_GATEWAY_IMAGE=ghcr.io/use-agent-os/agent-os:latest
AGENTOS_AUTH_TOKEN=$(openssl rand -hex 32)
EOF
docker compose up -d
```

[Run the gateway in a container](container-gateway.md) covers the rest:
providers, reaching it from another machine, connecting the desktop app,
upgrades and backups.

Without `AGENTOS_GATEWAY_IMAGE`, the Compose setup runs an `agentos:local`
image that you build yourself. Build it from a source checkout, after pulling
the Git LFS embedding-model files (see [Install from source](#install-from-source)
for the clone and `git lfs pull` steps):

```sh
docker build -t agentos:local .
```

`./start.sh` (or `start.ps1` on Windows) then runs `docker compose
up -d` and shows the gateway logs. Docker means you don't need
Python installed on your computer — pull the published image or build
one locally first.

## Troubleshooting

### Windows: `DLL load failed` / Visual C++ runtime

If the startup log shows `DLL load failed while importing
onnxruntime_pybind11_state`, AgentOS still works — but the built-in
local memory embedding (the on-device ONNX model) stays off and
memory falls back to keyword search until you install the Visual C++
Redistributable for Visual Studio 2015–2022 (x64).

The Windows portable launcher and the from-source PowerShell
installer both try to install this for you, using `winget`. If you
used Quick terminal install, or if `winget` is not available on your
computer, install it yourself and then restart PowerShell:
<https://aka.ms/vs/17/release/vc_redist.x64.exe>.
After that, restart the gateway:

```powershell
agentos gateway restart
```
