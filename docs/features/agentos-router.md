# AgentOS Router

AgentOS Router is AgentOS's local model-routing layer. It helps the agent
choose an appropriate model tier for each turn so routine work does not always
run on the most expensive model.

Use this page when you want to enable routing, understand what it changes, or
decide whether a fixed provider/model is better for a specific run.

## Why Use It

AgentOS Router is useful when you want:

- lower cost for simple chat, edits, summaries, and routine tool work;
- stronger models reserved for hard reasoning, recovery, and long tasks;
- one AgentOS workflow that can route across provider profiles;
- local routing decisions without sending prompts to a separate external
  classifier just to choose the model.

It is not required. AgentOS can also run in direct single-model mode.

## Strategies

AgentOS Router has three selectable strategies, set via
`agentos_router.strategy` in `agentos.toml` (or the onboarding wizard):

| Strategy | Mode label | How it decides |
| --- | --- | --- |
| `v4_phase3` (default) | Smart routing (on-device) | An on-device ML ensemble (BGE embeddings + LightGBM) scores each turn locally — no LLM call, nothing leaves your machine. The ~75MB model bundle is **not** distributed with the repo or the wheel yet, and the installers do not fetch it. When it is missing, the router logs a warning at boot and pins every turn to the default tier (c1) instead of failing the turn. To enable per-turn routing, restore the bundle into `src/agentos/agentos_router/models/v4.2_phase3_inference/`, or switch to `llm_judge` (which needs no local model files). |
| `pilot-v1` | Local ML — English-optimized (Pilot) | An AgentOS-native, English-optimized local router (MiniLM embeddings + a self-trained AgentOS model, ONNX). Like `v4_phase3` it decides on-device with no LLM call, nothing leaving your machine. See [The Pilot strategy](#the-pilot-strategy) below for status, config, and rollback. |
| `llm_judge` | Smart routing (LLM-based) | A small "judge" model classifies each turn (R0–R3) via a forced tool call. The judge can be a cloud model (default: the cheapest tier of your active provider) or a local OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, vLLM) configured with `judge_model` / `judge_base_url`. |

Both the Web UI setup wizard and the CLI (`agentos onboard`,
`agentos configure router`) offer a Mode dropdown with four options:
**Smart routing (on-device)**, **Local ML — English-optimized (Pilot)**,
**Smart routing (LLM-based)**, and **Off**. The "Judge model" field only
appears when the LLM-based strategy is selected; the "Pilot safety net" field
only appears when the Pilot strategy is selected — each is irrelevant to the
other strategies.

### The Pilot strategy

`pilot-v1` is an AgentOS-native, English-optimized local router. It replaces
the borrowed `v4_phase3` embedding+ensemble with a self-trained AgentOS model
(MiniLM embeddings + ONNX inference) that runs entirely offline — no LLM call,
nothing leaves your machine.

**Status (Phase A): opt-in.** `v4_phase3` remains the **default** until Pilot
passes an explicit evaluation gate. Pilot ships behind config so you can try it
without affecting the default install; switching is a single config line.

Enable it via the strategy id:

```toml
[agentos_router]
strategy = "pilot-v1"

[agentos_router.pilot]
# Under-routing safety-net floor. The effective cutoff is
# max(safety_net_threshold, router.confidence_threshold), so a value below the
# confidence threshold has no effect. Default 0.5.
safety_net_threshold = 0.5
```

or from the Web UI setup wizard / CLI by selecting the
**Local ML — English-optimized (Pilot)** router mode.

**Degrade behavior.** Like `v4_phase3`, Pilot never fails the turn if its
artifacts are missing. When the Pilot model bundle is not present, the strategy
tags the decision `pilot_unavailable` and routes the turn to the default tier
(the same graceful degrade `v4_phase3` uses when its bundle is missing).

**Rollback.** Reverting to the default is one config line — set
`strategy = "v4_phase3"` (or remove the `strategy` line) and restart. In
Phase A the `v4_phase3` bundle is still distributed, so rollback is always
available.

## Enable Routing

Recommended first-run setup:

```sh
agentos onboard --router recommended
```

Reconfigure an existing install:

```sh
agentos configure router --router recommended
```

Use the OpenRouter mixed defaults:

```sh
agentos configure router --router openrouter-mix
```

Disable routing and use the configured provider/model directly:

```sh
agentos configure router --router disabled
```

## Inspect Provider Support

Check the provider catalog available in your install:

```sh
agentos providers list
```

If the gateway is running, inspect runtime provider health:

```sh
agentos providers status
```

Router-supported profiles depend on the installed AgentOS version,
optional dependencies, and configured provider credentials. Common profiles
include OpenRouter (the default), Bankr, OpenAI, DeepSeek, Gemini,
DashScope, Moonshot, Volcengine, Zhipu, and compatible provider tiers exposed
by the local catalog.

## What the Router Can Affect

Depending on configuration, AgentOS Router may influence:

- selected model tier;
- direct model fallback;
- reasoning level;
- response policy;
- image-capable model selection;
- cache-continuity safeguards for recent higher-tier turns.

The exact decision is available through runtime metadata and diagnostics
surfaces. Turn on diagnostics when you need to understand why a turn was routed
to a particular model:

```sh
agentos diagnostics on
```

## Recommended Operating Modes

| Goal | Suggested mode |
| --- | --- |
| General personal-agent use | `recommended` |
| Multi-provider cost optimization through OpenRouter | `openrouter-mix` |
| Provider evaluation, billing audit, or reproducible benchmark run | `disabled` |
| Debugging one provider-specific behavior | `disabled` |

For routine use, start with `recommended`. Disable routing only when the model
choice itself is the thing you are testing.

This table covers the install/provider profile (`--router`). It is
independent of the `strategy` choice above — both `v4_phase3` and
`llm_judge` work under any profile.

## Example Requests

Good router-friendly requests describe the outcome, not the tier:

```text
Summarize this long issue thread and list the decision points.
```

```text
Review my current diff and point out the highest-risk changes.
```

Avoid asking the router to behave like a manual model picker unless you are
debugging:

```text
Use exactly this one model for every turn.
```

For exact-model work, configure direct routing instead.

## Troubleshooting

If routing does not appear to work:

1. Confirm the router is enabled:

   ```sh
   agentos config get router.enabled
   agentos config get llm.provider
   ```

2. Check provider readiness:

   ```sh
   agentos providers status
   agentos doctor
   ```

3. If AgentOS Router optional ML dependencies (`lightgbm`, `joblib`,
   `scikit-learn`, `onnxruntime` — install via `uv sync --extra recommended`
   or the `ml-router` extra) or the local model bundle are missing, the
   `v4_phase3` strategy degrades to the default tier rather than failing the
   turn (the `pilot-v1` strategy degrades the same way — `pilot_unavailable`
   → default tier — when its model bundle is missing); AgentOS can also still
   run with direct single-model routing, or switch `strategy` to `llm_judge`
   to route without any local ML bundle at all. On Windows, ONNX Runtime may
   require the Visual C++ Redistributable.

4. If you need deterministic model behavior for a run, disable routing:

   ```sh
   agentos configure router --router disabled
   ```

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
