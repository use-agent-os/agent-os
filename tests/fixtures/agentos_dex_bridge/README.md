# AgentOS DEX bridge fixtures

Recorded 2026-11-22 from `https://agg.useagentos.dev/v1/bridge/*`
(`docs/bridge.md`). The taker is the placeholder address from the gateway's
own API docs; it holds nothing, which is why `issues` reports a shortfall.

- `quote_*.json`: firm quotes (with `taker`) for the four routes the engine
  serves, plus one indicative quote (no `taker`).
- `error_no_route.json`: `NO_BRIDGE_ROUTE` for an amount too small to carry.
- `error_pair.json`: `UNSUPPORTED_BRIDGE_PAIR` for ETH into the USD stable.
- `status_filled.json`: `/v1/bridge/status` for a delivered transfer.

The deadline in each `transaction.data` is the quote's `expiresAt`: tests
pass a `now` just before it.
