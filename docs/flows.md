# Image Flows

An **Image Flow** is a small node graph that makes images: a prompt goes in,
optionally a prompt writer or an image description in between, one or more
image model calls, and a Save node that writes PNG files. You build flows in
the desktop app (**Flows** in the sidebar, after Jobs); the gateway stores
and runs them, so the same flow can be run again from the editor, from the
terminal (`agentos flows run`) or by an agent (`flow_run`).

Two things make a flow worth having over a one-off `image_generate` call:

- **It is reusable.** A flow keeps its prompt, model, aspect, count and the
  wiring between steps. Its *public inputs* (for example `prompt` and
  `count`) are the values a run may override; everything else stays as you
  set it.
- **It is cached.** Every node's result is cached by what went into it. Run a
  flow again with one input changed and only the nodes downstream of that
  input call a model again; the rest are reported as `cached` and cost
  nothing.

## Concepts

- **Links** carry one of two types: `TEXT` or `IMAGE` (a list of 1..n
  images). An input that has a type can be linked; when it is, the link wins
  over the input's own value. Numbers, choices and toggles are values only.
- **A run** has targets: the node you asked for (`--to NODE`) or else every
  node with no outgoing link. Only the targets and what they need run.
- **Run status** is `queued`, `running`, `done`, `failed` or `cancelled`.
  Each node is `queued`, `running`, `cached`, `done`, `error` or `skipped`.
  When a node fails, nodes already calling a model finish (and are cached),
  nothing new starts, the rest are `skipped`, and the run is `failed`.
- **Runs queue** first in, first out; `[flows].max_concurrent_runs` (default
  1) run at once, at most 2 calls per image provider. A run still queued or
  running when the gateway stops ends `failed` with `flows.interrupted`.

### Nodes

| Node | Inputs | Output | Notes |
| --- | --- | --- | --- |
| `text` | `text` | `TEXT` | A fixed piece of text. |
| `load_image` | `image` (an uploaded image) | `IMAGE` | Upload in the editor or drop a PNG. |
| `join_text` | `a`, `b` (optional), `template` (default `{a}, {b}`) | `TEXT` | A missing `b` drops the dangling `, `. |
| `prompt_writer` | `idea`, `context` (optional, link only), `style` | `TEXT` | Turns a short idea into a detailed image prompt with the auxiliary model (task `flow_prompt`). Paid. |
| `describe_image` | `image`, `focus` | `TEXT` | Describes an image with the vision model the `image` tool uses (task `vision`). Paid. |
| `image` | `prompt`, `images` (optional, several links), `model`, `aspect` / `resolution` / `size` / `quality` / `background` (shown when the model has them), `count` (1-4), `variation` | `IMAGE` | Text to image with nothing linked to `images`; edit / multi-reference with 1..n images linked. `variation = new` always calls the model again; `keep` reuses the cached result for the same inputs. Paid. |
| `save` | `images` (several links), `name` (default `{flow}_{run}_{n}`) | none | Writes PNG files that carry the flow inside them (an `agentos-flow` text chunk with the flow id, revision, run id and graph; never a key). |

`agentos flows nodes` lists them from the running gateway.

### Models and keys

`agentos flows models` lists every image model with whether it can be used,
why not, and its price per image. A model is available when its provider has
an API key; `[image_generation].enabled` only gates the `image_generate`
agent tool and does not affect flows. Keys and base URLs come from
`[image_generation.providers.*]`:

| Provider | Key (default env) | Models |
| --- | --- | --- |
| Surplus Intelligence | `SURPLUS_API_KEY` (`[image_generation.providers.surplus]`) | Nano Banana 2 / 2.1 / Pro, GPT Image 2, Seedream, FLUX.2 Pro, Qwen Image (from Surplus's public catalog, refreshed every 6 h, with a bundled snapshot as fallback). |
| OpenRouter | `OPENROUTER_API_KEY` | Nano Banana 2 / 2.1 / Pro, GPT-5 Image Mini. |
| OpenAI | `OPENAI_API_KEY` | GPT Image (`openai/gpt-image-1`): sizes, qualities, transparent background. |

Set a key with `agentos env set SURPLUS_API_KEY` (or the desktop's
Settings). A node's `model = auto` means `[image_generation].primary` when
that names an available flow model (the built-in default `openai/gpt-image-1`
does not count as a choice), else the first available model in the
order above (Surplus first), else none: an `image` node then fails
validation with "No image model is available" before anything is called.

### Costs

Prices are the best known public per-image prices and are marked
approximate (`~`). An estimate is per node: `per_image × count + per_ref ×
linked images`; the run's total adds only the nodes that will run and are
not cached. A node's recorded cost is what the provider reported when it
reports one, else the estimate. Costs are booked in the usage tracker under
the scope `flow:<run_id>` (see `agentos cost`).

## Running flows from the terminal

```sh
agentos flows list                                   # saved flows, newest first
agentos flows show "Lighthouse study"                # nodes, links, public inputs
agentos flows nodes                                  # node types
agentos flows models                                 # image models, keys, prices
agentos flows run "Lighthouse study" --input prompt="a red fox in snow" --input count=2
agentos flows run flw_2af4f4b5 --to n2 --json        # only up to node n2
agentos flows run flw_2af4f4b5 --no-wait             # queue and return the run id
agentos flows runs "Lighthouse study"                # recent runs of one flow
agentos flows stop run_0123456789ab
```

`run` resolves the flow by id, or by exact name (ids win; two flows with the
same name are refused, pass the id). It waits for the run (polling
`flows.run.get` every second, `--timeout` seconds, default 600) and prints
the status, node progress and cached count, the cost against the estimate
(written like the editor: `free`, `~$0.067`, `~$0.0034–$0.10`, `unknown`),
and the saved files:

```text
run_0123456789ab  Lighthouse study  done  3/3 nodes, 1 cached
cost $0.050 (estimate ~$0.067)
1 image(s):
  ~/.agentos/flows/outputs/flw_2af4f4b5/Lighthouse-study_run_0123456789ab_1.png  1024x1024
```

Exit codes: 0 when the run is `done`; 1 when it failed, was cancelled or is
still going at the timeout (it keeps running; check `agentos flows runs`);
2 for an input the engine refuses (unknown flow, a name that is not a public
input, a graph that does not validate: each node error is printed). With
`--json`, stdout is one summary object (`run_id`, `status`, `images[]` with
`path`, `sha256`, `width`, `height`, `cost_usd`, `est_usd`, `cached`,
`nodes {done,total}`, `estimate` (the total Price), `error`, `node_errors`,
`timed_out`) and errors are a
JSON envelope on stderr. Runs started here carry `origin: "cli"` and show up
live in the editor.

Like every gateway-backed command, `agentos flows` connects to
`AGENTOS_GATEWAY_URL`, else the port in the file `AGENTOS_GATEWAY_CONFIG_PATH`
names, else `ws://localhost:18791/ws`; `AGENTOS_STATE_DIR` alone does not
move it (point `AGENTOS_GATEWAY_URL` at a gateway on another port).

Running a flow spends money, so `flows run` and `flows stop` are the user's
actions: from an agent's shell (an agent-bound connection) the gateway refuses
them with `flows.operator_required`. Agents use `flow_run` instead.

## Agents: `flow_list` and `flow_run`

Two built-in tools let an agent use the flows you built. Both are in the
deferred set (the model loads them with `tool_search`), and both are offered
only while `[flows].enabled` is on and at least one image model has a key.

- `flow_list()` returns each flow's id, name, node count and public inputs
  with their current values, plus the default model and the agent cost limit.
- `flow_run(flow, inputs?, wait_seconds?)` runs a flow by id or exact name,
  with `inputs` overriding public inputs only. The tool descriptions steer the
  model to `flow_run` when you ask to run or reuse a flow you built, and to
  `image_generate` for a one-off picture. When `image_generate` is not
  available (off, or no key) but flows are, the system prompt tells the model
  that it can make the picture you ask for by running one of your flows, and
  that it should ask you to create one in the Flows editor when none fits;
  the tools never create a flow themselves. Their descriptions carry the
  words image, picture, draw, photo and illustration, so `tool_search` finds
  them for an image request.

**The cost gate.** `flow_run` asks the engine for the estimate before
anything is queued. A known estimate at or under
`[flows].agent_cost_limit_usd` (default $0.50) runs at once. A higher
estimate, or one that cannot be known, goes through the approval queue: you
see "Runs the Image Flow 'Lighthouse study' for an estimated ~$0.27, above the
$0.50 agents may spend per run without asking" in the same prompt that guards
shell commands, and the turn waits for you. The approval is bound to that
flow, its revision and those inputs; the run is then capped at the approved
amount (if the estimate rose in between, nothing is queued and the agent is
told). The prompt has its own approval namespace, `flow`, and offers only
allow once or deny: no "Always Allow" and no "Stop Asking" (the gateway
ignores both for this namespace), so approving one run never unlocks the
next, and a session bypass of shell prompts never approves a spend.

**A "no" sticks.** Denied, the agent gets a refusal that tells it plainly not
to retry or work around it (fewer images, other inputs, a cheaper model,
another flow, `image_generate`) unless you ask, and nothing is spent. For the
next 30 minutes every `flow_run` of that flow in that session needs a fresh
approval, even one under the limit; the prompt says "You declined a run of
the Image Flow ... N min ago". A prompt the agent stopped waiting for counts
when you decline it later. Approving such a prompt lifts the block. Other
sessions and other flows are not affected; the memory is in the gateway
process, so a gateway restart clears it. (A new chat message is not taken as
consent on its own: whether it "asks again" cannot be told reliably, so the
fresh approval prompt is the consent.)

The approval mode applies as usual (`auto-approve` runs without a prompt,
`auto-deny` refuses and counts as a "no"); an unattended run (cron,
heartbeat) refuses instead of waiting. Direct tool calls over HTTP
(`/tools/invoke`) never run `flow_run`.

The tool waits for the run (`wait_seconds`, default 300, at most 900). A run
still going then keeps running; the tool says so and the card updates when it
ends. Stopping the turn stops its run.

**What lands in the chat.** Each PNG a Save node writes is published into the
session as an image artifact while the run executes (channels deliver them
like any generated image), and a run card is published ahead of them:

- **Run card** (`application/vnd.agentos.flow-run+json`): the flow name, the
  status, node progress `n/m` with a bar, the cost (or the estimate while it
  runs), the cached count and thumbnails. The card re-reads the run with
  `flows.run.get` whenever it is drawn and follows the `flows.run` /
  `flows.node` events while the run is going, so a reload or a re-render
  never shows a stale snapshot. Thumbnails come from `flows.media` (256 px).
  It is read-only. On the desktop it has **Open in Flows**, which opens the
  flow in the editor; the web console has no editor and no button.

The tool's result (what the model reads) carries `status`, `run_id`, `flow
{id, name}`, `images[]` (`path`, `sha256`, `width`, `height`,
`artifact_id`), `cost_usd`, `est_usd`, `cached`, `nodes {done, total}`,
`error`, `node_errors` and the published `artifacts`.

### The card payload

```json
{
  "version": 1,
  "kind": "flow_run",
  "run_id": "run_0123456789ab",
  "flow_id": "flw_2af4f4b5",
  "flow_name": "Lighthouse study",
  "status": "done",
  "origin": "agent",
  "progress": {"done": 3, "total": 3},
  "cached": 1,
  "est_usd": 0.0672,
  "cost_usd": 0.05,
  "images": [{"sha256": "…", "mime": "image/png", "width": 1024, "height": 1024,
              "name": "Lighthouse-study_run_0123456789ab_1.png"}],
  "error": null,
  "created": "2026-10-29T10:00:00Z",
  "ended": "2026-10-29T10:00:21Z",
  "fetchedAt": "2026-10-29T10:00:21Z"
}
```

`images` lists what the Save nodes wrote, or the generated images when the
flow has no Save node (`name` is then null). `progress.done` counts `done`
and `cached` nodes. A client that has the run (`flows.run.get`) derives the
same fields from the RunRecord.

## Configuration

```toml
[flows]
enabled = true              # off: runs are refused (editor, CLI, agent tools) and the tools are hidden
max_concurrent_runs = 1     # 1-8; further runs wait in a FIFO queue
agent_cost_limit_usd = 0.50 # an agent's flow_run above this (or unknown) needs approval
cache_max_mb = 500          # node cache size, least recently used evicted first
```

Environment overrides: `AGENTOS_FLOWS_ENABLED`, `AGENTOS_FLOWS_MAX_CONCURRENT_RUNS`,
`AGENTOS_FLOWS_AGENT_COST_LIMIT_USD`, `AGENTOS_FLOWS_CACHE_MAX_MB`. For
example `agentos config set flows.agent_cost_limit_usd 1.00`.

State lives under `<state_dir>/flows/` (`~/.agentos/flows/` by default):
`<flow_id>.json` flow documents, `flows.db` (runs and the node cache),
`media/` content-addressed images (and their thumbnails),
`surplus_catalog.json`, and `outputs/<flow_id>/` the PNG files Save writes.

## RPC and events

Control surface (`flows.*`). Reads are open to every control connection;
everything that writes or spends (`create`, `save`, `delete`, `run`, `stop`,
`upload`, `import_png`) is refused on an agent-bound connection with
`flows.operator_required`.

| Method | Params | Result |
| --- | --- | --- |
| `flows.nodes` | – | node catalog (types, inputs, outputs, prices) |
| `flows.models` | – | `{default, models[]}` with availability and prices |
| `flows.list` | – | `{flows: [{id, name, rev, updated, node_count, preview}]}` |
| `flows.create` | `{name, template?}` (`blank`, `text_to_image`, `edit_image`, `describe_remix`) | `{flow}` |
| `flows.get` | `{id}` | `{flow}` |
| `flows.save` | `{flow, base_rev}` | `{flow}`; `flows.conflict` when the stored revision moved |
| `flows.delete` | `{id}` | `{ok}` |
| `flows.validate` | `{flow, to_node?}` | `{ok, node_errors}` |
| `flows.estimate` | `{flow, to_node?, inputs?}` | `{nodes, total, will_run, cached}` |
| `flows.run` | `{flow_id, flow?, inputs?, to_node?, origin?}` (`canvas` or `cli`) | `{run_id, status: "queued"}` |
| `flows.stop` | `{run_id}` | `{ok}` |
| `flows.runs` | `{flow_id?, limit?}` | `{runs: [RunSummary]}` newest first |
| `flows.run.get` | `{run_id}` | `{run: RunRecord}` (summary + inputs, graph, nodes, outputs, to_node, estimate, seq) |
| `flows.media` | `{sha256, max_side?}` | `{mime, data_b64, width, height}` |
| `flows.upload` | `{name, mime, data_b64}` | `{image}` |
| `flows.import_png` | `{data_b64}` | `{image, flow}` (the graph a saved PNG carries, not saved) |

Error codes: `flows.not_found`, `flows.conflict`, `flows.invalid`
(`details.node_errors`), `flows.busy`, `flows.unavailable`, `flows.cost_gate`
(`details.estimate`, `details.limit_usd`), `flows.operator_required`.

Events go to control connections; each carries `run_id`, `flow_id` and a
per-run increasing `seq`. Clients that reconnect read `flows.run.get`.

- `flows.run` `{run_id, flow_id, seq, status, est_usd?, cost_usd?, error?}`
- `flows.node` `{run_id, flow_id, seq, node_id, state, progress?, ms?, cost_usd?, outputs?, error?}`
- `flows.changed` `{flow_id, rev, deleted?}` after a create, save or delete.
