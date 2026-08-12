# Agentic Trading

AgentOS treats trading as a first-class capability. Market research, portfolio
analysis, and order execution reach the agent through partner-authorized
connections and skills that ship with the product, so you do not have to wire up
an unofficial API or paste credentials into a chat window.

Two mechanisms provide it:

- **MCP connections** — a featured Robinhood Trading preset in the Web UI, and
  any other MCP server you choose to add.
- **Bundled skills** — trading, research, and liquidity skills installed with
  AgentOS and loaded only when a task needs them.

Both run behind the same approval and sandbox layers as every other tool.

> Agentic trading involves significant risk. Review a connection's access and
> action permissions before authorizing it, and read
> [`../approvals-and-permissions.md`](../approvals-and-permissions.md) before
> letting any automation place orders.

## Robinhood Trading MCP

The Web UI ships a featured preset for Robinhood Agentic Trading.

| Setting | Value |
| --- | --- |
| Endpoint | `https://agent.robinhood.com/mcp/trading` |
| Transport | Streamable HTTP |
| Authorization | Provider-hosted OAuth flow |

Open **Settings > MCP Servers**, select the Robinhood Trading preset, and save
it. Saving opens the provider authorization flow and loads the server's tools
without a gateway restart.

For scripted deployments, declare the same server in TOML:

```toml
[mcp]
enabled = true
connect_timeout_seconds = 10

[[mcp.servers]]
name = "robinhood-trading"
transport = "streamable_http"
url = "https://agent.robinhood.com/mcp/trading"
oauth = true
tool_timeout_seconds = 30
```

See [`../configuration.md`](../configuration.md) for the full MCP configuration
reference and [`../web-ui.md`](../web-ui.md) for the connection UI.

### Account scope

Robinhood's own documentation defines the product scope; the live authenticated
MCP schemas are authoritative for tool names, parameters, supported order types,
and available asset classes.

- The connected agent can **read** all Robinhood accounts, including account
  numbers, positions, balances, transaction and order history, watchlists, and
  scans.
- Trade **placement** is restricted to the dedicated Robinhood Agentic account,
  even though read access is broader.

### How the agent is expected to behave

The bundled `robinhood-agentic-trading` skill encodes the operating rules:

1. Confirm the connected server is the intended Robinhood Trading endpoint.
2. Complete the provider-hosted authentication flow. Never ask for OAuth codes,
   access tokens, account numbers, or credentials in chat.
3. Discover the current tools and read their complete input schemas before
   planning calls. Never invent a tool name, parameter, enum value, order type,
   or asset class.
4. Re-discover tools after reconnecting, or when a schema error indicates that
   capabilities changed.
5. If the connection, authentication, or a required tool is unavailable, stop
   and explain the gap rather than substituting an unofficial API.

## Bundled trading and research skills

These install with AgentOS and load on demand. Skills marked
`risk: high` declare `signing` capability and require explicit confirmation
before they execute anything.

| Skill | What it does |
| --- | --- |
| `robinhood-agentic-trading` | Account and portfolio analysis, market research, order preview, placement, cancellation, rebalancing, and bounded automation over the Trading MCP. |
| `robinhood-rwa-addresses` | Resolves a company name or ticker to its Robinhood tokenized-asset token on Robinhood Chain (`4663`) — symbol, contract address, chain id, decimals. Public data source, no key. |
| `gmgn-swap` | Buy and sell tokens on Solana, BSC, Base, and Ethereum: single swap, multi-wallet batch, limit orders, stop loss, take profit, and trailing variants. |
| `gmgn-market` | Price charts (K-line, OHLCV), trending rankings by volume, new launchpad listings, and hot-search rankings. |
| `gmgn-token` | Per-token research: price, market cap, liquidity, holders, top Smart Money and KOL positions, security audit, and social links. |
| `gmgn-portfolio` | Wallet analysis: holdings, realized and unrealized P&L, win rate, history, and developer-wallet token history. |
| `gmgn-track` | Real-time buy and sell activity from Smart Money wallets, KOL wallets, and wallets you follow. |
| `gmgn-holder-analysis` | Holder-structure analysis: chip distribution, entry cost, whale and dev behavior, and risk wallets. |
| `gmgn-cooking` | Token creation and launchpad launches, and launchpad creation statistics. |
| `senior-unilp-manager` | Uniswap V4 liquidity on Base (`8453`) and Robinhood Chain (`4663`): inspect pools and positions, then mint, increase, decrease, collect fees, or burn. |

The GMGN skills require a `gmgn-cli` install plus `GMGN_API_KEY` and
`GMGN_PRIVATE_KEY`. The signing key authorizes orders — treat it as a
credential. See [`skills.md`](skills.md) for how skill requirements and secrets
are declared and surfaced.

## Local by default

AgentOS runs on your own machine. For trading, that changes three things.

**Credentials stay on the device.** MCP authorization tokens are written to a
`0700` directory on POSIX systems; Windows uses the current user's
state-directory ACL. The GMGN request-signing key (`GMGN_PRIVATE_KEY`) and the
Uniswap LP signing key (`UNIV4_LP_PRIVATE_KEY`) are local environment values.
Nothing is escrowed with a hosted service, and the agent never asks you to paste
a credential into chat.

**The routing decision never leaves the machine.** The Pilot Router classifies
each turn on-device, so a strategy prompt is not shipped to a third party merely
to decide which model should handle it. Only the turn that actually runs reaches
your chosen model provider. See
[`agentos-router.md`](agentos-router.md) for the classifier and its
alternatives.

**Always-on work runs on hardware you already have.** Scheduled and recurring
trading tasks execute through the local gateway rather than a rented agent
cloud — see [`../scheduling.md`](../scheduling.md). Combined with router-driven
model selection, running an agent continuously stays inexpensive.

What still leaves the machine is exactly what has to: the broker or exchange API
calls themselves, and the model turns you route to a remote provider.

## Safety model

Trading skills are held to a stricter standard than ordinary tools.

- **Explicit confirmation.** Skills tagged `[FINANCIAL EXECUTION]` do not act on
  an inferred intent; they require the user to confirm the specific action.
- **Dry run by default.** `senior-unilp-manager` splits reads from writes. Its
  read script cannot sign at all, and a write is a dry run unless it is given
  both `--broadcast` and the plan hash printed by that same dry run.
- **Schema truth.** The agent plans against the live authenticated schemas of
  the connected server, not remembered API shapes.
- **Publisher verification.** A skill cannot mint its own brand. Only bundled
  skills that ship inside the wheel may name a publisher, and the displayed
  name, URL, and logo always come from an internal allowlist — currently
  Robinhood, Bankr, and Capminal. Every other skill takes its brand from the
  catalog row that installed it, so a look-alike dropped on disk renders as an
  ordinary unbranded skill.
- **Standard approvals.** Everything above sits on top of the normal permission
  and sandbox layers described in
  [`../approvals-and-permissions.md`](../approvals-and-permissions.md) and
  [`../tools-and-sandbox.md`](../tools-and-sandbox.md).

## Related

- [`../web-ui.md`](../web-ui.md) - connecting and authorizing MCP servers.
- [`../configuration.md`](../configuration.md) - MCP and skill configuration.
- [`skills.md`](skills.md) - skill discovery, install, and authoring.
- [`../approvals-and-permissions.md`](../approvals-and-permissions.md) -
  permission tiers and approval prompts.
