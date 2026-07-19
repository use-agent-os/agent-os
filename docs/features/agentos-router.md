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
| `pilot-v1` (default) | Local ML — English-optimized (Pilot) | An AgentOS-native, English-optimized local router (MiniLM embeddings + a self-trained AgentOS model, ONNX). Decides on-device with no LLM call, nothing leaving your machine. The bundle ships in the wheel under `src/agentos/agentos_router/models/pilot_v1/`; a missing bundle degrades to the default tier (c1). See [The Pilot strategy](#the-pilot-strategy) below for status, config, and rollback. |
| `v4_phase3` (legacy) | Smart routing (on-device) | The previous default: an on-device ML ensemble (BGE embeddings + LightGBM) scoring each turn locally — no LLM call. Fully selectable and the one-line rollback from `pilot-v1`. The bundle ships in the wheel under `src/agentos/agentos_router/models/v4.2_phase3_inference/`; when missing, the router logs a warning at boot and pins every turn to the default tier (c1) instead of failing. To restore it, `git lfs pull` the bundle or switch to `llm_judge` (which needs no local model files). |
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

**Status: default strategy.** `pilot-v1` is the default router strategy — a
fresh install routes through it with no config change. It was promoted from
opt-in after passing the owner's relative-to-incumbent ship gate (it beats the
`v4_phase3` incumbent on 11/12 evaluation axes; see `DATA.md` /
`eval_report.md`). `v4_phase3` remains fully selectable as the legacy fallback.

The default needs no config, but the Pilot safety-net floor is tunable:

```toml
[agentos_router]
# strategy = "pilot-v1"  # default — this line is optional

[agentos_router.pilot]
# Under-routing safety-net floor. The effective cutoff is
# max(safety_net_threshold, router.confidence_threshold), so a value below the
# confidence threshold has no effect. Default 0.5.
safety_net_threshold = 0.5
```

The Web UI setup wizard / CLI preselect the
**Local ML — English-optimized (Pilot)** router mode by default.

**Degrade behavior.** Like `v4_phase3`, Pilot never fails the turn if its
artifacts are missing. When the Pilot model bundle is not present (e.g. a source
checkout without `git lfs pull`), the strategy tags the decision
`pilot_unavailable` and routes the turn to the default tier (the same graceful
degrade `v4_phase3` uses when its bundle is missing).

**Rollback to v4.** Reverting to the legacy router is one config line — set
`strategy = "v4_phase3"` (in `[agentos_router]`) and restart. The `v4_phase3`
bundle still ships in the wheel, so rollback is always available.

## One Router, One Provider

Routing is **single-provider**: the gateway builds one provider client from
`[llm].provider` at boot, and tiers only choose which **model** each turn
uses. The `provider` field on a tier is descriptive metadata — it never makes
a request reach a different provider. Configure every tier with a model that
`[llm].provider` itself serves.

Local providers (Ollama, LM Studio, OVMS, vLLM) have no built-in tier
profile. Onboarding writes self-consistent single-model tiers for them; to
get real multi-model routing, edit the tiers to point at other models your
local server has pulled (see the local example in `agentos.toml.example`).
If a tier still points at a different provider than `[llm].provider` — for
example leftover cloud defaults on an Ollama install — the router degrades
that route to `[llm].model` instead of sending the local server a model name
it does not have; the turn metadata carries `routing_degraded: true`,
`agentos doctor` reports the mismatch, and the gateway logs a one-time
warning at boot.

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
