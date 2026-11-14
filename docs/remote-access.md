# Remote Access

The gateway controls chat, tools, sessions, channels, approvals and
configuration, all with your provider credentials. It therefore listens on
loopback (`127.0.0.1:18791`) by default, and the safest way to reach it from
another machine is to keep it there and bring the connection to it.

Use this page when you want the Web UI, the desktop app, the CLI or an MCP
client on one machine to drive a gateway running on another. For a gateway in
a container, follow [Run the gateway in a container](container-gateway.md): the
steps start from the published image and differ in a few places.

## Pick a Path

| Path | Gateway bind | Who authenticates | Good for |
| --- | --- | --- | --- |
| [SSH tunnel](#ssh-tunnel) | loopback | your SSH key | any host you can SSH into; nothing to install |
| [Tailscale Serve](#tailscale-serve) | loopback | your tailnet identity, plus the gateway token | your own devices, from anywhere, over HTTPS |
| [Reverse proxy](#reverse-proxy-with-trusted-proxy-mode) | loopback or LAN | the proxy (SSO, mTLS, basic auth) | a team entry point you already run |
| [Token on a LAN bind](#token-on-a-lan-or-public-bind) | `0.0.0.0` or a LAN IP | the gateway token | a trusted home or lab network |

Whatever you choose, run `agentos doctor` afterwards: it reports the risky
combinations (`gateway.bind_no_auth`, `gateway.allowed_origins_wildcard`,
`gateway.cors_wildcard_with_credentials`, `gateway.trusted_proxy_unset`,
`gateway.auth_rate_limit_off`,
`gateway.rate_limit_loopback_exempt_behind_proxy`, `gateway.token_short`)
with the command that fixes each. It finds the token on its own
(`AGENTOS_GATEWAY_TOKEN`, then `auth.token` in the config, or `--token`), and
when no gateway answers it checks the config file instead.

## Writing the Config

Every recipe below changes settings in the gateway's config file. Use
`agentos config set <key> <value> --config <file>`: **without `--config` the
command only prints an `export AGENTOS_GATEWAY_…` line and changes nothing**
for a gateway started anywhere else. The examples write
`~/.agentos/config.toml`, the file a gateway reads by default (under
`AGENTOS_STATE_DIR` when that is set); if your gateway was started with
`--config <file>` or from a directory with an `agentos.toml`, use that file
instead (`agentos doctor` names it). Editing the TOML by hand works just as
well. The gateway reads the file at startup, so restart it afterwards:

```sh
agentos gateway restart
```

Configure authentication **before** you expose the gateway, never after:
until the restart the old settings are live.

## SSH Tunnel

The universal path. The gateway stays on loopback on the remote host; SSH
forwards a local port to it:

```sh
ssh -N -L 18791:127.0.0.1:18791 user@host
```

Then use the gateway as if it were local:

```text
http://127.0.0.1:18791/control/
```

```sh
agentos gateway status --gateway ws://127.0.0.1:18791/ws
```

`-N` opens no shell; leave the command running for as long as you need the
tunnel. To keep it up across drops, use your SSH client's keep-alive options
or a tool such as `autossh`. If port 18791 is taken locally, forward another
one (`-L 18800:127.0.0.1:18791`) and open `http://127.0.0.1:18800/control/`.

No AgentOS configuration changes: to the gateway the browser is a loopback
peer, so the default `auth.mode = "none"` keeps working. Set a token anyway
if other people have accounts on the remote host.

The desktop app works through the tunnel as well: Settings › Gateway ›
Connection, **Run mode** External, **Protocol** HTTP, host `127.0.0.1`,
port `18791` (or the local port you forwarded), plus the token if one is
set. See [Desktop app → Using a gateway on another
machine](desktop-app.md#using-a-gateway-on-another-machine).

## Tailscale Serve

[Tailscale Serve](https://tailscale.com/kb/1312/serve) publishes a loopback
service to your tailnet over HTTPS with a `*.ts.net` certificate. The gateway
keeps its loopback bind. Serve connects to it from loopback, so the gateway
must enforce a token first; without one every device on your tailnet would be
admitted as if it sat at the keyboard.

1. Enforce a token, allow the tailnet origin, and let the rate limiter see
   the real client. On a loopback bind the gateway accepts browser requests
   only from loopback origins (the guard against cross-site WebSocket
   hijacking and DNS rebinding), so the Serve origin must be listed. Serve
   adds `X-Forwarded-For`; trusting it as a local proxy makes failed-token
   lockouts apply per tailnet device instead of exempting everything that
   arrives from 127.0.0.1:

   ```sh
   CFG=~/.agentos/config.toml
   agentos config set auth.mode token --config "$CFG"
   agentos config set auth.token "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" --config "$CFG"
   agentos config set control_ui.allowed_origins '["https://<machine>.<tailnet>.ts.net"]' --config "$CFG"
   agentos config set auth.trusted_proxy 127.0.0.1 --config "$CFG"
   agentos gateway restart
   ```

   Check it took effect before going further: `agentos doctor` must not
   report `gateway.rate_limit_loopback_exempt_behind_proxy`, and
   `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18791/api/sessions`
   must print `401`.

2. Only now publish it:

   ```sh
   tailscale serve --bg --https=443 http://127.0.0.1:18791
   ```

Open `https://<machine>.<tailnet>.ts.net/control/`, enter the token on
the Connect screen (or in Settings › Gateway) and press Connect. The desktop app
connects the same way: **Run mode** External, **Protocol** HTTPS, host
`<machine>.<tailnet>.ts.net`, port `443`, and the token. The same settings
reach a gateway behind a [reverse proxy](#reverse-proxy-with-trusted-proxy-mode),
with that proxy's hostname; whichever path you take, the origin the app
presents is the gateway's own, so it must be declared on the gateway as
above (`control_ui.allowed_origins` or `[gateway] public_origin`).
`tailscale serve status` shows what is published; `tailscale serve --https=443
off` takes it down. Do not use Tailscale **Funnel** for the gateway: it
publishes the port to the whole internet.

If you would rather not trust the proxy's header, lock out loopback too
instead (`agentos config set auth.rate_limit.exempt_loopback false --config
"$CFG"`); a local script with a stale token is then locked out like anyone
else.

## Reverse Proxy with Trusted-Proxy Mode

When a reverse proxy already authenticates your users (SSO, mTLS, basic
auth), let it be the only thing that can reach the gateway and tell the
gateway to trust it:

```toml
host = "127.0.0.1"            # or the interface the proxy connects from

[auth]
mode = "trusted-proxy"
trusted_proxy = "127.0.0.1"   # IPs or CIDR blocks, comma-separated

[control_ui]
allowed_origins = ["https://agent.example.com"]
```

Write those keys with `agentos config set … --config <file>` or edit the
file, then restart. In this mode a request is admitted only when the
*transport peer* (the address the TCP connection comes from, never a header)
is listed in `auth.trusted_proxy`. Anything else gets `401`. The mode is accepted on a
non-loopback bind for that reason; with an empty `trusted_proxy` nothing is
admitted and `doctor` reports `gateway.trusted_proxy_unset`.

The proxy must forward WebSocket upgrades on `/ws` and should append the
client address to `X-Forwarded-For`. The origin you list in
`control_ui.allowed_origins` (or `[gateway] public_origin`) is also the
hostname the gateway answers to: a request whose `Host` names anything else
gets `400` (the DNS-rebinding guard), so a proxy that forwards the public
`Host` needs that origin declared. The gateway reads that header only from
a trusted peer and walks it right to left, skipping trusted hops, so the
first address your proxies did not add is the client used for rate limiting.
An address that names the gateway host itself (loopback or one of its own
interfaces) is treated as spoofed and ignored. List every proxy hop, for
example `trusted_proxy = "10.0.0.0/8"` behind a load balancer.
`agentos gateway run` turns uvicorn's own proxy-header handling off, so the
`FORWARDED_ALLOW_IPS` environment variable has no effect: `X-Forwarded-For`
never changes which peer admission sees.

If the proxy instead forwards to a gateway in `token` mode on the same host,
set `auth.trusted_proxy` to the proxy's address anyway: otherwise every client
it forwards looks like a loopback peer, and loopback is exempt from the
failed-token lockout (`doctor` reports
`gateway.rate_limit_loopback_exempt_behind_proxy`).

A minimal Caddy site:

```text
agent.example.com {
    forward_auth ... # your authentication
    reverse_proxy 127.0.0.1:18791
}
```

A minimal nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:18791;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

## Token on a LAN or Public Bind

Binding beyond loopback requires an enforced auth mode; the gateway refuses
to start with `auth.mode = "none"` on `0.0.0.0` or a LAN address. Set the
mode in the file first, then start on the wider bind:

```sh
agentos config set auth.mode token --config ~/.agentos/config.toml
agentos gateway run --bind-mode lan      # same as --listen 0.0.0.0
```

`--bind-mode tailnet` listens on the machine's `tailscale ip -4` address and
on `127.0.0.1` only, and refuses to start when Tailscale is not up instead of
falling back to every interface. Browsers are admitted from the address you
opened the console at when it is a private one (`http://192.168.1.20:18791`),
from the gateway's own localhost, and from declared origins; anything else is
refused before auth. To open it under a DNS name, declare that origin:
`agentos config set gateway.public_origin https://agent.example.com --config
~/.agentos/config.toml`. See [Gateway → Browser origins](gateway.md#browser-origins).

A token is generated at startup when none is set. Clients send it as
`Authorization: Bearer <token>` (HTTP) or in the WebSocket `connect`
request; it is never accepted in a query string. Wrong tokens are throttled
per client address: ten failures in a minute lock that address out for five
minutes (`429` with `Retry-After` over HTTP, `AUTH_RATE_LIMITED` over the
WebSocket), whatever `Origin` header the attempts carry. Tune it under
`[auth.rate_limit]`; keep it on for any non-loopback bind. An address that
already holds 128 sockets in the handshake gets `HTTP 403` for the next one
until some of them finish.

Only do this on a network you trust. The traffic is plain HTTP unless you
also set `[tls] certfile` and `keyfile`.

## Rotating the Token

Generate a new token, restart, then update every client:

```sh
agentos config set auth.token "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" --config ~/.agentos/config.toml
agentos gateway restart
```

Use at least 24 random characters (`doctor` warns below that as
`gateway.token_short`). Clients holding the old token start getting
`UNAUTHORIZED`, and repeated attempts count against the failed-auth limit, so
update scripts and the desktop app's gateway settings promptly.

## What Never to Do

- Do not bind `0.0.0.0` with `auth.mode = "none"`. The startup guard refuses
  it; do not work around the guard.
- Do not forward the gateway port to the internet from your router, or
  publish it with a public tunnel, without an authenticating proxy in front.
- Do not put `"*"` in `control_ui.allowed_origins`, and do not combine
  `cors.allowed_origins = ["*"]` with `cors.allow_credentials = true` on a
  non-loopback bind.
- Do not use `trusted-proxy` mode unless the listed addresses really are
  your proxies, and the gateway port is reachable only through them.
- Do not put the token in a URL; use the header.

Read next:

- [`gateway.md`](gateway.md)
- [`http-api.md`](http-api.md)
- [`configuration.md`](configuration.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
