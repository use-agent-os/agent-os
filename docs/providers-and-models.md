# Providers and Models

AgentOS supports multiple LLM providers through one configuration surface.
You can run direct single-model mode or enable Pilot Router for tiered routing.

Use this page when you need to configure a provider, inspect model support, or
choose between direct model mode and router mode.

## Inspect Providers

List provider metadata from the local install:

```sh
agentos providers list
agentos providers list --json
```

Show runtime provider diagnostics from the running gateway:

```sh
agentos providers status
agentos providers status openrouter --json
agentos providers status --probe-models
```

`providers list` does not require a running gateway. `providers status` does.

## Configure a Provider

Interactive (the provider steps of `agentos onboard`, skipping channels and
search):

```sh
agentos configure provider
```

`agentos onboard` runs the full first-run wizard, which also covers channels and
search.

Non-interactive onboarding-style configuration:

```sh
export OPENROUTER_API_KEY="sk-..."
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY

export OPENCAP_API_KEY="ocap_..."
agentos configure provider --provider opencap --api-key-env OPENCAP_API_KEY

export SURPLUS_API_KEY="inf_..."
agentos configure provider --provider surplus --api-key-env SURPLUS_API_KEY
```

When testing OpenCAP from a source checkout, prefix both configuration and
gateway commands with `uv run` (for example, `uv run agentos gateway restart`).
This keeps the config writer and restarting gateway on the same local schema
instead of invoking an older globally installed AgentOS.

Direct provider examples:

```sh
agentos configure provider --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
agentos configure provider --provider anthropic --model claude-sonnet-4-6 --api-key-env ANTHROPIC_API_KEY
agentos configure provider --provider gemini --model gemini-2.5-flash --api-key-env GEMINI_API_KEY
agentos configure provider --provider ollama --model llama3.1
```

Prefer environment-variable references for API keys so secrets are not written
directly into configuration files.

Once a provider has a key or key reference configured, switch the active
provider and model without prompts:

```sh
agentos providers configure openrouter --model anthropic/claude-sonnet-4
```

`providers configure` never prompts and has no `--api-key-env`; set the key
with `agentos configure provider` first.

## Onboarding-Verified Providers

This build exposes onboarding support for:

- OpenRouter (default provider)
- Bankr LLM Gateway
- OpenCAP
- Surplus Intelligence
- OpenAI
- Anthropic
- Ollama
- DeepSeek
- Gemini
- DashScope / Qwen
- Moonshot AI
- Zhipu / Z.AI
- Baidu Qianfan
- Volcengine Ark

The provider registry may contain additional compatible providers for advanced
or self-hosted setups. Use `agentos providers list` on your install for the
current catalog.

### OpenCAP routing

OpenCAP defaults to `https://gw.capminal.ai/api/inference/v1` and uses one
OpenAI-compatible key for inference. Its public model catalog is unauthenticated.
The default direct/fallback model is the balanced `c1` model, `gpt-6-luna`. The `recommended`
router profile selects bare OpenCAP model IDs across
[`c0`–`c3`](features/agentos-router.md#model-tiers) and the vision route:

| Tier | Model | Role |
| --- | --- | --- |
| `c0` | `deepseek-v4.1-flash` | trivial chat, short rewrites, extraction |
| `c1` | `gpt-6-luna` | default balanced route for normal agent work |
| `c2` | `glm-5.3` | multi-step coding, structured reasoning, larger synthesis |
| `c3` | `claude-opus-5.5` | difficult planning, deep review, high-stakes synthesis |
| `image_model` | `minimax-m3` | image attachments, screenshots, diagrams |

These are OpenCAP's own defaults, not a copy of the Bankr profile — the two
gateways publish overlapping but different catalogs. Run `agentos models list`
against a configured OpenCAP key to see everything the gateway currently serves.

At gateway boot, AgentOS fetches the public catalog asynchronously for model
choices, capabilities, and provider-scoped cost estimates. If that fetch fails,
configured models can still run with static capability and cost fallbacks.

OpenCAP chooses the cheapest eligible upstream when no route is configured.
To restrict one model to a supported upstream, use bare model IDs and an
upstream provider ID advertised by the current live catalog:

```toml
[llm.provider_routing]
"glm-5.3" = "provider-id-from-live-catalog"
```

AgentOS sends this as OpenCAP's provider allow-list. OpenRouter uses the same
configuration table but retains its existing preferred-order payload.

### Surplus Intelligence routing

[Surplus Intelligence](https://www.surplusintelligence.ai/) is a two-sided
marketplace for inference: sellers list endpoints, buyers get routed to the
cheapest healthy one, and settlement happens in USDC on Base. AgentOS talks to
it as an ordinary OpenAI-compatible provider using a buyer API key (`inf_...`),
so no wallet or on-chain payment support is required.

It defaults to `https://api.surplusintelligence.ai/v1`. Its model catalog is
public and unauthenticated. The default direct/fallback model is the balanced
`c1` model, `gpt-6-luna`. The `recommended` router profile selects bare
Surplus model IDs across
[`c0`–`c3`](features/agentos-router.md#model-tiers) and the vision route:

| Tier | Model | Role |
| --- | --- | --- |
| `c0` | `deepseek-v4.1-flash` | trivial chat, short rewrites, extraction |
| `c1` | `gpt-6-luna` | default balanced route for normal agent work |
| `c2` | `glm-5.3` | multi-step coding, structured reasoning, larger context |
| `c3` | `claude-opus-5.5` | difficult planning, deep review, high-stakes synthesis |
| `image_model` | `glm-5.3-flash` | image attachments, screenshots, diagrams |

The image tier differs from the OpenCAP profile: Surplus publishes `minimax-m3`
without vision in its supported features, and `glm-5.3-flash` is both
vision-capable and the cheapest such route it serves.

Because prices move with seller competition, cost estimates come from the live
catalog rather than a static table. At gateway boot AgentOS fetches it once and
reuses that response to seed pricing; the cache refreshes on its own TTL
afterwards. Set `AGENTOS_SURPLUS_LIVE_PRICING=0` to pin estimates to the static
table instead. If the fetch fails, configured models still run on static
capability and cost fallbacks.

Surplus routes to the cheapest eligible seller when no route is configured. To
restrict one model to particular sellers, use bare model IDs and a seller ID
advertised by the current live catalog:

```toml
[llm.provider_routing]
"glm-5.3" = "seller-id-from-live-catalog"
```

AgentOS sends this as Surplus's provider allow-list, the same shape OpenCAP
takes.

## Model Inspection

List models:

```sh
agentos models list
```

If runtime-backed model inspection cannot connect, start the gateway:

```sh
agentos gateway run
```

For provider metadata that does not require the gateway, use:

```sh
agentos providers list
```

## Direct Model vs Router

Direct model mode:

```sh
agentos configure router --router disabled
agentos configure provider --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
```

Router mode:

```sh
agentos configure router --router recommended
```

| Mode | Use when |
| --- | --- |
| Direct model | You are testing one exact model, reproducing provider behavior, or auditing provider billing. |
| Router mode | You want normal personal-agent use where cost and task complexity vary by turn. |

For routing details, see
[`features/agentos-router.md`](features/agentos-router.md).

## Provider Health Circuit Breaker

Failover is health-aware. When the active provider returns consecutive
provider-health failures — overload / gateway 5xx, transport errors, or rate
limits — AgentOS opens a circuit breaker for that provider and sends the next
turns straight down the fallback chain instead of paying the dead provider's
timeout on every turn.

- `failure_threshold` consecutive failures open the breaker (default `3`).
  Request-shaped failures (unknown model, bad request, context overflow, auth,
  billing) never count — they say nothing about provider health.
- While open, the provider is skipped for `cooldown_seconds` (default `60`).
  Each consecutive trip doubles the window up to `max_cooldown_seconds`
  (default `600`).
- After the cooldown, exactly one turn is admitted as a half-open probe. A
  clean turn closes the breaker; another failure re-opens it with the longer
  window.
- If every provider in the chain is in cooldown, the primary is used anyway —
  a provider in cooldown still beats no provider at all.
- A per-turn model override (an explicit `model`, or one picked by the router)
  applies to the primary link. While the primary's breaker is open the turn
  runs on the fallback's own configured model instead — the same semantics as
  an ordinary failover, but now for the whole cooldown window rather than only
  after a live failure.

Tune or disable it in `agentos.toml`:

```toml
[llm.circuit_breaker]
enabled = true
failure_threshold = 3
cooldown_seconds = 60
max_cooldown_seconds = 600
```

Inspect the current state:

```sh
agentos providers status          # "circuit" column: closed | half_open | open (42s)
agentos doctor                    # provider.circuit.open / provider.circuit.half_open
curl localhost:8787/api/system/status   # circuitBreaker / circuitBreakers
```

Breaker state lives in the running gateway only; restarting clears it.

### Rate limits that say when to come back

A 429 (or an overload) whose response names a wait is backpressure, not an
outage. AgentOS reads that wait from `Retry-After` (delta-seconds or an
HTTP-date), `retry-after-ms`, or, when neither is sent, the reset header of
whichever limit is spent (OpenAI's `x-ratelimit-reset-requests` /
`x-ratelimit-reset-tokens`, Anthropic's `anthropic-ratelimit-*-reset`). Waits
are clamped to 0–300 s.

- When the wait is at most 30 s, the turn sleeps exactly that long (plus up to
  10 % jitter, capped at 1 s) and retries the **same** model, so the prompt
  cache and the price stay as they were. Each wait counts against the agent's
  provider retries (`--max-provider-retries`, default `3`); once they are spent
  the turn fails over to the next model in the chain.
- A longer wait fails over straight away, as before.
- A rate limit with a short wait does not count toward the circuit breaker.
  One without a wait, or with a longer one, still does, and so does an
  overload / 5xx whatever it says.
- A rate limit with no wait at all keeps the exponential backoff.

The 30 s cap is `AgentConfig.rate_limit_retry_after_cap_seconds` for embedded
callers; `0` turns the behaviour off. It has no `agentos.toml` key.

## Repetitive Output Guard

A model occasionally gets stuck writing the same line, paragraph or short
phrase over and over. Left alone it streams until `max_tokens`, which costs
tokens and time and leaves a wall of repeated text. AgentOS watches the visible
text and, separately, the reasoning of every response while it streams:

- Nothing is checked until the text passes `first_check_chars` (default
  `16000`). After that a check runs each time the text has grown by as much
  again (16k, 32k, 64k, then every 64k), and each check reads only the last
  `tail_chars` (default `64000`), so the cost stays flat however long the reply
  gets.
- A check trips when the tail repeats itself exactly — the same unit at
  least `min_repeats` times in a row (default `5`), found from a
  `window_chars`-long anchor (default `60`) — or when one line repeats that
  often with only small changes between its copies, and in either case the
  loop fills at least `min_coverage` of the tail (default `0.5`).
- On a trip the engine stops reading and closes the provider stream, keeps the
  text up to the end of the first repeated copy, drops every tool call the cut
  response had started (none of them run), and logs
  `provider.repetition_detected`. The response is then continued once, like a
  response cut by the output limit, and the client gets a
  `repetition_detected` warning. A second loop in the same step ends the turn
  with the error code `repetition_detected`.
- The saved reply has the repeated part removed; a client that rendered the
  stream live already showed it until the cut.

Long legitimate output — a big table, generated code that repeats the same
boilerplate, a numbered list of hundreds of items — differs from row to row, so
it is never exactly periodic and does not trip. A response that really should
repeat one block verbatim many times (a zero-filled matrix, a template printed
50 times) can trip; raise the thresholds or turn the guard off:

```toml
[llm.repetition_guard]
enabled = true
first_check_chars = 16000
tail_chars = 64000
window_chars = 60
min_repeats = 5
min_coverage = 0.5
```

```sh
agentos config set llm.repetition_guard.enabled false
```

A cut response never reaches the provider's usage event, so AgentOS counts an
estimate for it instead: input from the request-size estimate, output from the
length of the text and reasoning it streamed (about four characters per
token). The estimate feeds the turn totals, the session usage and the
`max_turn_input_tokens` / `max_turn_output_tokens` budgets; with no
provider-billed figure the turn's cost is the pricing-table estimate.

## Provider Troubleshooting

Start with:

```sh
agentos doctor
agentos providers status
agentos diagnostics on
```

Check:

- the API key environment variable is set in the gateway process environment;
- the model id matches the provider;
- the base URL is correct for compatible APIs;
- proxy settings match your network;
- router is disabled when debugging one exact provider/model;
- the provider's circuit breaker is not open (`agentos providers status`);
- the gateway was restarted after config changes.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
