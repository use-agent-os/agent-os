# Pilot Router

Pilot Router is AgentOS's local model-routing layer. It helps the agent
choose an appropriate model tier for each turn so routine work does not always
run on the most expensive model.

Use this page when you want to enable routing, understand what it changes, or
decide whether a fixed provider/model is better for a specific run.

**Naming.** "Pilot Router" names the whole routing layer — every strategy
below runs inside it. `pilot-v1` is one specific strategy: the default local
ML model that ships with Pilot Router. Wire identifiers keep their original
names (the `[agentos_router]` config section and the `AGENTOS_ROUTER_` env
prefix).

## Why Use It

Pilot Router is useful when you want:

- lower cost for simple chat, edits, summaries, and routine tool work;
- stronger models reserved for hard reasoning, recovery, and long tasks;
- one AgentOS workflow that can route across provider profiles;
- local routing decisions without sending prompts to a separate external
  classifier just to choose the model.

It is not required. AgentOS can also run in direct single-model mode.

## Model Tiers

Pilot Router sorts every turn into one of four text tiers, ordered from
cheapest and fastest to strongest, plus a separate vision route. `c1` is the
default tier: it handles ordinary work, and it is where the router falls back
whenever it is unsure or its model bundle is unavailable.

| Tier | Handles |
| --- | --- |
| `c0` | Trivial chat, short rewrites, extraction, and low-risk simple Q&A. |
| `c1` | Normal agent work: coding assistance, debugging, and moderate analysis. **Default tier.** |
| `c2` | Multi-step coding, structured reasoning, larger-context synthesis, and harder analysis. |
| `c3` | Difficult planning, deep review, complex debugging, and high-stakes synthesis. |
| `image_model` | Vision route for user-supplied image attachments, screenshots, and diagrams. |

**`image_model` is not a difficulty level.** It is chosen when the turn carries
image attachments, independently of the `c0`–`c3` classification.

**Each provider profile maps its own models.** The tier is the routing
decision; which model serves that tier comes from `[agentos_router.tiers]`, and
the defaults differ per provider profile. See the annotated tier block in
`agentos.toml.example`, or the Providers and Models page for the profiles
themselves.

**Classes and tiers are the same four levels.** The classifier reports
`R0`–`R3`; these map one-to-one onto `c0`–`c3`. Diagnostics output and the LLM
judge use the `R` names, while config, the CLI, and this page use the `c` names.

**Reasoning effort follows the tier** when `auto_thinking` is enabled: `c3`
raises thinking to T3, `c2` to T2, `c1` to T1, and `c0` runs with no thinking
budget.

### How a Turn Lands on a Tier

The tier for a turn is not only the classifier's opinion. These steps apply in
order, and a later one can override an earlier one:

1. **Image attachments** route the turn to `image_model` outright, skipping
   text classification;
2. **a manual pin** (`/c0` … `/c3`) overrides classification, and stays in
   effect until you run `/auto`, leave the session, or the hold idles out after
   ten minutes;
3. **the strategy classifies** the current message into `R0`–`R3` and maps it to
   the matching tier — only the current message is classified, not the history;
4. **a low-confidence decision snaps back to the default tier** — when the
   strategy's confidence falls below `confidence_threshold` (default `0.5`), a
   non-default tier is replaced by `c1`;
5. **a short complaint upgrades one tier** — a brief frustrated follow-up (at
   most `complaint_upgrade_max_chars`, default 160) raises the turn by
   `complaint_upgrade_steps` (default 1);
6. **cache continuity holds a higher tier** — when a recent turn (within
   `kv_cache_anti_downgrade_window_seconds`, default 600) ran higher, the turn
   is not downgraded below it;
7. **a translation request drops to a ceiling** — see
   [Translation requests](#translation-requests) below;
8. **a large context raises a floor** — roughly 25,000 material tokens floors
   the turn at `c2`, and roughly 80,000 tokens, or 40% of the model's context
   window, floors it at `c3`.

To see which of these applied to a given turn, turn on diagnostics as described
in [What the Router Can Affect](#what-the-router-can-affect).

### Translation Requests

The strategies score *reasoning difficulty*, not task type, so an ordinary
"translate this paragraph" is scored as ordinary work and lands on `c1` — and
because the Pilot corpus is English-only, the same request written in another
language drifts a tier in either direction for no reason but its script.

Whether translation deserves more than the cheapest model is a policy question
rather than something a classifier can be trained into, so it is answered
deterministically. A translate verb in the first or last paragraph of a turn
— recognised in English, Vietnamese, Chinese, Japanese, Korean, Thai,
Indonesian, French, Spanish, German, Portuguese, Russian, Arabic, Hindi,
and Dutch —
caps the turn at `translate_ceiling_tier` (default `c0`).

| Key | Default | Meaning |
| --- | --- | --- |
| `translate_ceiling_enabled` | `true` | Turn the cap off entirely. |
| `translate_ceiling_tier` | `c0` | Tier a translation turn is capped at. |

Every detected translation is capped, extras and all: asking for commentary
alongside the translation, or for a poem's rhyme to survive it, does not lift
the turn. Three things still do:

- **a complaint upgrade**, so a follow-up saying the last answer was wrong is
  never capped in the same turn;
- **the large-context floor**, which runs after the cap and lifts a document
  too big for the cheap tier back to one that can hold it;
- **a programming language as the target** — "translate this Python module to
  Rust" is a request to write code, not to translate prose, and is the one
  phrasing that shares the verb without sharing the task.

Detection reads only the leading and trailing paragraph of a turn, so a pasted
document that merely mentions translation is not treated as a translation
request. When a turn is capped, `routing_extra` carries `task_type`,
`task_type_ceiling_from_tier`, and the matched language; when the verb matched
but the cap did not apply, `task_type_blocked_by` records why.

## Strategies

Pilot Router has two selectable strategies, set via
`agentos_router.strategy` in `agentos.toml` (or the onboarding wizard):

| Strategy | Mode label | How it decides |
| --- | --- | --- |
| `pilot-v1` (default) | Local ML — English-optimized (Pilot) | An AgentOS-native, English-optimized local router (MiniLM embeddings + a self-trained AgentOS model, ONNX). Decides on-device with no LLM call, nothing leaving your machine. The bundle ships in the wheel under `src/agentos/agentos_router/models/pilot_v1/`; a missing bundle degrades to the default tier (c1). See [The Pilot strategy](#the-pilot-strategy) below for status, config, and upgrade-from-v4 behavior. |
| `llm_judge` | Smart routing (LLM-based) | A small "judge" model classifies each turn (R0–R3) via a forced tool call. The judge can be a cloud model (default: the cheapest tier of your active provider) or a local OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, vLLM) configured with `judge_model` / `judge_base_url`. |

Both the Web UI setup wizard and the CLI (`agentos onboard`,
`agentos configure router`) offer a Mode dropdown with three options:
**Local ML — English-optimized (Pilot)**, **Smart routing (LLM-based)**, and
**Off** — the legacy **Smart routing (on-device)** (`v4_phase3`) option is no
longer offered. The "Judge model" field only
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
`v4_phase3` incumbent on 11/12 evaluation axes; see
`scripts/pilot_router/DATA.md` / `scripts/pilot_router/eval_report.md`). The legacy `v4_phase3` engine and its ~52MB model bundle
have been removed from the tree entirely (Phase C); a config that still pins it
is auto-migrated to `pilot-v1` on the next load (see **Upgrading from
v4_phase3**).

Those reports measure routing *quality* and feature-extraction *latency*. For
what the router has saved you in dollars on your own traffic, see
[`agentos cost savings`](../cli.md#agentos-cost-savings) — it rolls the
per-turn savings telemetry in `~/.agentos/logs/decisions-*.jsonl` up into a
summary, JSON/CSV, or a shareable PDF.

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
degrade `v4_phase3` used when its bundle was missing).

**Upgrading from v4_phase3.** Historical installs persisted
`strategy = "v4_phase3"` explicitly in `~/.agentos/config.toml`. On the next
config load AgentOS **automatically migrates** any such config to `pilot-v1`:
the old file is backed up verbatim next to it (`config.toml.backup.<timestamp>`)
and rewritten with `strategy = "pilot-v1"`, and the flip is logged. The
migration is idempotent — once rewritten there is nothing left to migrate. There
is no way to keep `v4_phase3` in config: the legacy engine and its model bundle
were removed from the tree (Phase C), and a value that bypasses the file
migration (e.g. an env override) normalizes to `pilot-v1` at config load.

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
include OpenRouter (the default), Bankr, OpenCAP, Surplus Intelligence, OpenAI,
DeepSeek, Gemini, DashScope, Moonshot, Volcengine, Zhipu, and compatible
provider tiers exposed by the local catalog.

## What the Router Can Affect

Depending on configuration, Pilot Router may influence:

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
independent of the `strategy` choice above — both `pilot-v1` and
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

3. If Pilot Router optional ML dependencies (`numpy`, `onnxruntime`,
   `tokenizers` — install via `uv sync --extra recommended`
   or the `ml-router` extra) or the local model bundle are missing, the
   default `pilot-v1` strategy degrades to the default tier rather than
   failing the turn (it tags the decision `pilot_unavailable` — the same
   graceful degrade the legacy `v4_phase3` used); AgentOS can also still
   run with direct single-model routing, or switch `strategy` to `llm_judge`
   to route without any local ML bundle at all. On Windows, ONNX Runtime may
   require the Visual C++ Redistributable.

4. If you need deterministic model behavior for a run, disable routing:

   ```sh
   agentos configure router --router disabled
   ```

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
