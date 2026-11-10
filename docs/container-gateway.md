# Run the Gateway in a Container

Run the AgentOS gateway from the published container image on a server, a VPS
or a home box, then use it from the [desktop app](desktop-app.md), the
[Web UI](web-ui.md) or the [CLI](cli.md) on your own machine.

Use this page when the gateway should keep running while your laptop is closed:
channels and scheduled jobs stay up, and sessions, memory, provider keys and
wallets live on the server. If you only want AgentOS on one Mac, the desktop app
runs a gateway for you and you do not need any of this.

The setup has four steps: start the container, give it a provider, choose how
your machine reaches it, and point the desktop app at it.

## Before You Start

- A Linux host (`amd64` or `arm64`) with Docker Engine and Compose v2. Check with
  `docker compose version`.
- About 1.4 GB of disk for the image, plus room for your state. The gateway idles
  around 200 to 250 MiB of memory and uses more during turns.
- An API key for an LLM provider.
- SSH access to the host. The first connection below uses nothing else.

## 1. Start the Gateway

Create a directory for the deployment and download the Compose file:

```sh
mkdir -p ~/agentos && cd ~/agentos
curl -fsSLO https://raw.githubusercontent.com/use-agent-os/agent-os/main/compose.yaml
```

Create a `.env` next to it with the image and a fresh token:

```sh
umask 077
cat > .env <<EOF
AGENTOS_GATEWAY_IMAGE=ghcr.io/use-agent-os/agent-os:latest
AGENTOS_AUTH_TOKEN=$(openssl rand -hex 32)
EOF
```

Start it and check that it answers:

```sh
docker compose up -d
docker compose ps
curl -s http://127.0.0.1:18791/ready
```

`/ready` answers with `"state":"ready"` within a few seconds. `docker compose ps`
reports `healthy` after the first health check, about 30 seconds in. Clients need
the token, so note where it lives: `grep AGENTOS_AUTH_TOKEN .env`.

Two things are worth knowing about this setup:

- **The gateway is not on the network yet.** Inside the container it binds
  `0.0.0.0`, because Docker port publishing needs that, but Compose publishes the
  port on the host's `127.0.0.1` only. Step 3 decides who else can reach it.
- **The token is required.** The gateway refuses to serve a wildcard bind without
  enforced auth, so `compose.yaml` turns token auth on and will not start without
  `AGENTOS_AUTH_TOKEN`.

Replace `latest` with a release version (`ghcr.io/use-agent-os/agent-os:2026.11.7`)
to choose when you upgrade. See [Upgrade](#upgrade).

## 2. Configure a Provider

A new gateway has no provider, so `agentos doctor` reports `Active provider is not
configured` until you set one. Pick whichever of these suits you.

**OpenRouter, from `.env`.** Add the key and recreate the container:

```sh
echo 'OPENROUTER_API_KEY=YOUR_API_KEY' >> .env
docker compose up -d
```

`compose.yaml` forwards only `OPENROUTER_API_KEY` and `BRAVE_SEARCH_API_KEY` from
`.env`. Any other name you add there stays out of the container unless you also
add it to the `environment:` block.

**Any provider, with the CLI in the container.** Store the key at a hidden prompt,
which keeps it out of your shell history, then select the provider:

```sh
docker compose exec gateway agentos env set OPENAI_API_KEY
docker compose exec gateway agentos configure provider --provider openai --api-key-env OPENAI_API_KEY --model YOUR_MODEL
docker compose exec gateway agentos gateway restart
```

Run `docker compose exec gateway agentos providers list` to see the provider names.

**From the desktop app or the Web UI**, once you have connected in step 4:
Settings › Providers works as it does locally. Restart the gateway on its host
afterwards if the pane asks for it.

Provider settings and stored keys persist on the volume. Check the result:

```sh
docker compose exec gateway agentos doctor
```

The first line should start with `AgentOS Doctor - Ready`.

## 3. Choose How to Reach It

| Path | Who can reach the gateway | What you change | Good for |
| --- | --- | --- | --- |
| [SSH tunnel](#ssh-tunnel) | anyone with your SSH key | nothing | getting started; any host |
| [Tailscale Serve](#tailscale-serve) | your tailnet, plus the token | one command on the host | your own devices, from anywhere |
| [HTTPS reverse proxy](#https-reverse-proxy) | the internet, plus the token | a Compose override and a DNS name | a stable address, phones, teammates |
| [LAN port](#lan-port) | your network, plus the token | the `ports:` line | a trusted home or lab network |

Start with the SSH tunnel. It proves the whole chain with no exposure, and you
can move to another path later without touching your data.

### SSH Tunnel

Run this on your own machine and leave it running:

```sh
ssh -N -L 18791:127.0.0.1:18791 user@your-server
```

The gateway now answers on `http://127.0.0.1:18791` on your machine. If that port
is taken locally, forward another one (`-L 18800:127.0.0.1:18791`) and use it in
the next step. To survive network drops, use your SSH client's keep-alive options
or `autossh`.

### Tailscale Serve

With Tailscale installed on the host, publish the loopback port to your tailnet:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:18791
```

The gateway accepts `*.ts.net` names without further configuration. Do not use
Tailscale **Funnel**, which publishes the port to the whole internet.

### HTTPS Reverse Proxy

Point a DNS name at the host and open ports 80 and 443. Next to `compose.yaml`,
create a `compose.override.yaml`, which Compose merges in automatically. This one
adds [Caddy](https://caddyserver.com/), which obtains and renews the certificate
on its own:

```yaml
services:
  gateway:
    environment:
      AGENTOS_GATEWAY_GATEWAY__PUBLIC_ORIGIN: https://agent.example.com
      AGENTOS_AUTH_TRUSTED_PROXY: 172.30.0.0/24
  caddy:
    image: caddy:2
    command: caddy reverse-proxy --from agent.example.com --to gateway:18791
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - caddy-data:/data
    restart: unless-stopped

networks:
  default:
    ipam:
      config:
        - subnet: 172.30.0.0/24

volumes:
  caddy-data:
```

Replace `agent.example.com` in both places, then recreate the stack. The network
settings changed, so take it down first. Leave off `-v`, which would delete your
data:

```sh
docker compose down
docker compose up -d
```

Check it from your own machine. The first command should print `200`, and the
second `401`, which proves the token is enforced:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://agent.example.com/healthz
curl -s -o /dev/null -w '%{http_code}\n' https://agent.example.com/api/sessions
```

What the two settings do:

- `AGENTOS_GATEWAY_GATEWAY__PUBLIC_ORIGIN` declares the address clients use. The
  gateway refuses any hostname it does not know (`400`) and any browser or app
  origin it does not know (`403`). It must match exactly, so a proxy on a port
  other than 443 needs that port in the origin as well.
- `AGENTOS_AUTH_TRUSTED_PROXY` lets the gateway read the real client address from
  `X-Forwarded-For`. Without it every client looks like the proxy, so ten wrong
  tokens from anyone lock everyone out for five minutes. The fixed subnet is what
  makes this setting possible; choose another one if `172.30.0.0/24` is in use on
  your host.

Already running a proxy? Send it to `127.0.0.1:18791` or attach it to the same
Docker network, and keep the two settings above, with the subnet the proxy
connects from. It must forward WebSocket upgrades on `/ws` and pass the public
`Host` header. A health probe has to send that `Host` too, or it gets `400` and
the proxy marks the gateway down. See
[Remote access](remote-access.md#reverse-proxy-with-trusted-proxy-mode).

### LAN Port

To publish the port on your network, edit the `ports:` line in `compose.yaml`:

```yaml
    ports:
      - "192.168.1.20:18791:18791"
```

Then run `docker compose up -d`. Traffic, including the token, is plain HTTP, so
do this only on a network you trust, and never on a public IP: use the reverse
proxy for that. Private addresses work with no other configuration. Docker's
published ports also bypass host firewalls such as `ufw`, so do not count on those
to restrict the port.

## 4. Connect the Desktop App

In the app, open Settings › Gateway › Connection. On a first launch you can get
there from the setup screen with **Connect to an existing gateway instead**, with
no engine install.

Set **Run mode** to **External**, fill in the values for your path, and press
**Save**:

| Path | Protocol | Host | Port |
| --- | --- | --- | --- |
| SSH tunnel | HTTP | `127.0.0.1` | `18791` (or the local port you forwarded) |
| Tailscale Serve | HTTPS | `your-machine.your-tailnet.ts.net` | `443` |
| HTTPS reverse proxy | HTTPS | `agent.example.com` | `443` |
| LAN port | HTTP | `192.168.1.20` | `18791` |

**Auth token** is the value of `AGENTOS_AUTH_TOKEN` from your `.env`, on every path.

The light in the sidebar footer and the Process row in Settings › Gateway show
whether the gateway is reachable. Send a message to confirm that the provider
works too.

What changes once the app is on an external gateway:

- **The container owns the data.** Sessions, memory, skills and wallets are the
  gateway's, on the server. Nothing in the app's local `~/.agentos` moves over or
  is deleted; switching back to **Managed** brings it back.
- **The app no longer runs the gateway.** It stops a gateway it had been running
  for itself. It cannot start or stop this one, and offers **Retry connection**
  when the gateway is unreachable.
- **Updates are separate.** The app still updates itself. For the gateway, the
  toolbar shows **Gateway outdated** when a newer release exists, and you
  [upgrade it on the host](#upgrade).
- **Wallets work.** A connection that authenticates with the gateway token from
  another machine counts as the operator, so you can create, import and unlock
  wallets and approve orders from the app. See
  [Trading](features/trading.md). Anyone holding the token can do the same, so
  guard it like a wallet password.

More on this mode is in [Desktop app](desktop-app.md#using-a-gateway-on-another-machine).

### Web UI and CLI

The same address and token work for the other clients. Open the Web UI at the
gateway's address (`http://127.0.0.1:18791/control/` through the tunnel) and enter
the token on the Connect screen (or in Settings › Gateway). For the CLI on your own
machine:

```sh
export AGENTOS_GATEWAY_TOKEN=YOUR_TOKEN
agentos gateway status --gateway ws://127.0.0.1:18791/ws
```

Use `wss://agent.example.com/ws` behind HTTPS.

## Day-to-Day Operations

Run these on the host, from the directory that holds `compose.yaml`.

| Task | Command |
| --- | --- |
| Follow the logs | `docker compose logs -f gateway` |
| Check health | `docker compose exec gateway agentos doctor` |
| Restart, letting turns finish | `docker compose exec gateway agentos gateway restart` |
| Run any CLI command | `docker compose exec gateway agentos ...` |
| Stop, keeping your data | `docker compose stop` |

### Changing Settings

`.env` and `compose.yaml` are read when the container is created, so changes there
need `docker compose up -d`. `docker compose restart` keeps the old values, which
is a common surprise after rotating a token.

Settings in the gateway's own config file persist on the volume. Name the file
explicitly, because `agentos config set` without `--config` only prints an
`export` line and changes nothing:

```sh
docker compose exec gateway agentos config set gateway.public_origin https://agent.example.com --config /var/lib/agentos/config.toml
docker compose exec gateway agentos gateway restart
```

### Rotating the Token

Put a new value in `.env`, recreate the container, and update every client,
including the desktop app's **Auth token**:

```sh
sed -i "s/^AGENTOS_AUTH_TOKEN=.*/AGENTOS_AUTH_TOKEN=$(openssl rand -hex 32)/" .env
docker compose up -d
```

### Upgrade

Do not run `agentos upgrade` in the container: the next recreate would throw that
away. Replace the image instead. Point `AGENTOS_GATEWAY_IMAGE` in `.env` at the new
release if you pinned one, then:

```sh
docker compose pull
docker compose up -d
```

State on the volume carries over. Docker gives a stopping container 10 seconds by
default, which can cut off turns still running; they are
[resumed on the next boot](gateway.md#interrupted-turns) where that is safe. To
give them the full drain window, add this to `compose.override.yaml`:

```yaml
services:
  gateway:
    stop_grace_period: 60s
```

Take a backup before a large version jump. A newer gateway may migrate the
database, so restoring the backup is the reliable way back to an older release.

### Back Up and Restore

Everything lives in one named volume mounted at `/var/lib/agentos`: config, the
database, sessions, memory, the workspace, logs, wallets and the home directory
with any tools you installed. Stop the gateway so
the database is consistent, then archive the volume:

```sh
docker compose stop gateway
docker compose run --rm --no-deps -u root -v "$PWD:/backup" --entrypoint sh gateway -c 'umask 077; tar czf /backup/agentos-state.tgz -C /var/lib/agentos .'
docker compose start gateway
```

The archive holds provider keys and encrypted wallets, so store it accordingly.
Keep your wallet password separately. To restore, on a new host or into an empty
volume:

```sh
docker compose run --rm --no-deps -u root -v "$PWD:/backup:ro" --entrypoint sh gateway -c 'tar xzf /backup/agentos-state.tgz -C /var/lib/agentos'
docker compose up -d
```

`docker compose down` keeps the volume. `docker compose down -v` deletes it, along
with any wallets inside that you have not backed up.

## What Is in the Image

The image runs as the non-root user `agentos` (uid `10001`), and holds the gateway,
the Web UI, the bundled router and embedding models, and the tools most agent work
reaches for: Python, a C toolchain, Node.js with `npm` and `npx`, `uv`, `git`, `gh`,
`curl`, `jq`, `ripgrep` and `tmux`.

**Anything else that installs without root goes on the volume.** The user's home is
`/var/lib/agentos/home`, so a tool installed with `npm install -g` or `uv tool
install`, and the login or config it keeps in its home directory, survives an
upgrade. Skills offer these installs from Settings › Skills. To install one yourself:

```sh
docker compose exec gateway npm install -g gmgn-cli
docker compose exec gateway agentos gateway restart
```

The restart is what makes the agent's prompt list a newly installed tool. These
tools count towards your backup, and they are yours to update: replacing the image
does not touch them.

The image does **not** include `ffmpeg`, LibreOffice, Chromium or bubblewrap, which
has consequences:

- **No operating-system sandbox.** With bubblewrap missing, the container itself is
  the boundary around the agent's shell and file tools. Check your posture with
  `docker compose exec gateway agentos sandbox status`, and read
  [Tools and sandbox](tools-and-sandbox.md). Do not mount the Docker socket or
  host directories you would not hand to the agent. A bind-mounted directory must
  be writable by uid `10001`.
- **Skills that need `ffmpeg` or LibreOffice stay unavailable.** They need system
  packages, which only a custom image can add.
- **The `browser` tool stays hidden.** See [Browser](features/browser.md).

To add what needs root, build on the published image and point
`AGENTOS_GATEWAY_IMAGE` at the result:

```dockerfile
FROM ghcr.io/use-agent-os/agent-os:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
USER agentos
```

```sh
docker build -t agentos-custom:local .
```

You can also build the base image from a source checkout with
`docker build -t agentos:local .`, after `git lfs pull` fetches the model files.
See [Install from source](https://github.com/use-agent-os/agent-os#install-from-source).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Compose stops with an error naming `AGENTOS_AUTH_TOKEN` | no `.env`, or you are in another directory | run from the directory holding `.env`, and set the token |
| Container restarts forever; logs say `Refusing to serve: auth.mode='none'` | a wildcard bind without auth, usually an older or custom Compose file | set `AGENTOS_AUTH_MODE: token` and `AGENTOS_AUTH_TOKEN` in `environment:` |
| `port is already allocated` | something else holds 18791 on the host | publish another host port, such as `127.0.0.1:18800:18791`, and use it in your clients |
| `401` or `UNAUTHORIZED` | the client's token differs from the running one | compare with `.env`; after editing `.env`, run `docker compose up -d` |
| `429` or `AUTH_RATE_LIMITED` | ten wrong tokens in a minute locked the address for five minutes | fix the token and wait; behind a proxy, set `AGENTOS_AUTH_TRUSTED_PROXY` so one client cannot lock out the rest |
| `400`, `Rejected Host header` | the gateway does not know that hostname | declare it with `AGENTOS_GATEWAY_GATEWAY__PUBLIC_ORIGIN`; make proxy health probes send the public `Host` |
| App stays disconnected, WebSocket gets `403` | the origin is not declared, or its port differs | make the public origin match the app's Protocol, Host and Port exactly |
| Proxy reaches `/healthz`, chat never connects | WebSocket upgrades are not forwarded | forward `Upgrade` and `Connection` on `/ws` |
| `doctor` reports `Active provider is not configured` | no key reached the gateway | [configure a provider](#2-configure-a-provider) and restart |
| A key in `.env` has no effect | `compose.yaml` forwards only the names it lists | add it to `environment:`, or store it with `agentos env set` in the container |
| Provider or channel change has not applied | those settings load at startup | `docker compose exec gateway agentos gateway restart` |

For anything else, start with `docker compose logs --tail 100 gateway` and
`docker compose exec gateway agentos doctor`, then see
[Troubleshooting](troubleshooting.md).

Read next:

- [`remote-access.md`](remote-access.md)
- [`desktop-app.md`](desktop-app.md)
- [`gateway.md`](gateway.md)
- [`configuration.md`](configuration.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
