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

Interactive:

```sh
agentos providers configure openrouter
```

Non-interactive onboarding-style configuration:

```sh
export OPENROUTER_API_KEY="sk-..."
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY

export OPENCAP_API_KEY="ocap_..."
agentos configure provider --provider opencap --model oc-uncensored-1.0 \
  --api-key-env OPENCAP_API_KEY
```

Direct provider examples:

```sh
agentos configure provider --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
agentos configure provider --provider anthropic --model claude-sonnet-4-6 --api-key-env ANTHROPIC_API_KEY
agentos configure provider --provider gemini --model gemini-2.5-flash --api-key-env GEMINI_API_KEY
agentos configure provider --provider ollama --model llama3.1
```

Prefer environment-variable references for API keys so secrets are not written
directly into configuration files.

## Onboarding-Verified Providers

This build exposes onboarding support for:

- OpenRouter (default provider)
- Bankr LLM Gateway
- OpenCAP
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
The default direct/fallback model is `oc-uncensored-1.0`. The `recommended`
router profile selects bare OpenCAP model IDs across C0-C3 and the vision route,
with `oc-uncensored-1.0` assigned to C0.

OpenCAP chooses the cheapest eligible upstream when no route is configured.
To restrict one model to a supported upstream, add a routing entry:

```toml
[llm.provider_routing]
"glm-5.2" = "surplus"
```

AgentOS sends this as OpenCAP's provider allow-list. OpenRouter uses the same
configuration table but retains its existing preferred-order payload.

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
- the gateway was restarted after config changes.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
