# Configuration

AgentOS can be configured from the onboarding wizard, the Web UI setup
flow, CLI commands, environment variables, and TOML files. Use CLI commands for
routine setup and edit TOML only for advanced or scripted deployments.

## Config Load Order

AgentOS reads configuration in this order:

1. `AGENTOS_GATEWAY_CONFIG_PATH`
2. `./agentos.toml`
3. `~/.agentos/config.toml`
4. built-in defaults

Use `--config ./agentos.toml` when you want to write or inspect a
project-local config file.

## Secret Handling

Prefer environment-variable references for secrets:

```sh
export OPENROUTER_API_KEY="sk-..."
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
```

Avoid committing raw API keys to TOML files, shell history, examples, or issue
reports.

## Environment Variables

Environment variables live in `~/.agentos/.env` and are managed with
[`agentos env`](cli.md#environment-variables) or the Environment screen in the
Web UI. They are where credentials belong — skills and external binaries read
them, and AgentOS masks them in every listing.

### Load order and the shadowing trap

`.env` files are read once at process start, in this order, and **an existing
environment variable is never overridden**:

1. `os.environ` — whatever the shell that started AgentOS exported
2. `$CWD/.env`, then `$CWD/.env.test`
3. `~/.agentos/.env`

The first rule is the one that surprises people. If your shell exported
`OPENAI_API_KEY`, then writing that variable through AgentOS updates the file
and the running process, but a restart goes back to the shell's value. `agentos
env list` reports the source of each value (`process env` / `project .env` /
`AgentOS .env`) and the Web UI warns on the row, so this is visible rather than
something to discover through a confusing hour. To make the file authoritative,
remove the export and restart.

Likewise, a `.env` in the directory the gateway was started from wins over
`~/.agentos/.env`.

### What cannot be written through AgentOS

Names that steer subprocess execution or AgentOS runtime posture are refused by
every AgentOS surface — the Web UI, the CLI, the RPC, and the agent tool:

- Loader and interpreter: `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`,
  `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `NODE_OPTIONS`, `NODE_PATH`
- Shell and implicitly-invoked commands: `PATH`, `SHELL`, `IFS`, `BASH_ENV`,
  `EDITOR`, `VISUAL`, `PAGER`, `BROWSER`, `GIT_SSH_COMMAND`, `GIT_EXEC_PATH`
- AgentOS posture and state location: `AGENTOS_SENSITIVE_PATHS_DISABLED`,
  `AGENTOS_SENSITIVE_PAYLOAD_DISABLED`, `AGENTOS_REDACT_SECRETS`,
  `AGENTOS_STRIP_PROVIDER_ENV`, `AGENTOS_SHELL_DENYLIST`, `AGENTOS_SAFE_BIN_*`,
  `AGENTOS_AGENT_PERMISSIONS`, `AGENTOS_TRUST_ENV`, `AGENTOS_HOOKS`,
  `AGENTOS_GATEWAY_TOKEN`, `AGENTOS_GATEWAY_CONFIG_PATH`, `AGENTOS_STATE_DIR`, `AGENTOS_ROOT`, and the
  bind settings
- Outbound routing: `AGENTOS_LLM_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY`,
  `ALL_PROXY`, `NO_PROXY` — **in any casing**, because the libraries that read
  these names lower-case them first, so `Http_Proxy` routes traffic exactly as
  `HTTP_PROXY` does. `AGENTOS_LLM_PROXY` is applied to every provider client,
  so a writable proxy name would let a surface hand each request — API key
  header included — to a machine of its choosing. `AGENTOS_TRUST_ENV`, which
  decides whether the ambient `*_PROXY` names are honoured at all, is refused
  for the same reason. Configure a proxy through `llm.proxy` in the config
  file, or export it in the shell that starts the gateway.

Tools AgentOS spawns inherit most of `os.environ`, and several AgentOS guards
are themselves read from it, so a surface that could write these names could
widen what the agent is allowed to do. The `AGENTOS_` prefix is not
blanket-blocked — ordinary credentials such as `AGENTOS_LLM_API_KEY` remain
writable.

### Credentials and child processes

`exec_command` and `background_process` pass most of the environment to the
command they run, so `gh`, `aws`, `docker` and the rest keep working as they do
in your own shell. Three groups are treated differently:

| Group | Reaches a child? | Why |
| --- | --- | --- |
| `AGENTOS_GATEWAY_TOKEN` and the guard switches above | never | The token authenticates to the control plane; the switches let a child reconfigure the next call. |
| AgentOS provider keys (LLM, search, image, audio, embeddings) | yes, unless you opt out | Bundled skills read them from `os.environ`. Set `AGENTOS_STRIP_PROVIDER_ENV=1` before starting AgentOS to withhold them. |
| Everything else | yes | Your own credentials, in your own shell. |

`execute_code` is the other way round: it forwards a small fixed allowlist and
nothing else. A skill that needs its own API key there declares it, and the
name is added for the session that loaded the skill:

```yaml
metadata:
  agentos:
    requires:
      env: [CAP_API_KEY]
```

The value is read from the environment at spawn time and never enters the
transcript. A skill installed from a hub cannot declare one of AgentOS's own
provider keys this way — that request is refused and logged.

Output from `exec_command`, `background_process` and `process(action=log)` is
scanned for credentials before it reaches the model. Vendor-shaped keys, auth
headers, JWTs, private keys and DSN passwords are masked. Set
`AGENTOS_REDACT_SECRETS=0` before starting AgentOS to turn this off; it is read
once at startup, so a command the agent runs cannot switch it off mid-session.

File content is scanned the same way: `read_file`, `read_spreadsheet`,
`grep_search` and `edit_file`'s closest-match hint mask credentials on their way
back to the model, using a `«redacted:sk-…»` sentinel that cannot be mistaken
for a usable key and written back over the real one. This layer stays on even
under `/elevated full`, where the sensitive-path denylist is lifted, so an
elevated read of a secrets file does not put the secrets verbatim into the
persisted transcript. Reading one through the shell (`cat ~/.aws/credentials`)
gets the same treatment.

How hard the pass looks depends on the file. Credentials recognisable from their
own text — vendor-prefixed keys, JWTs, PEM private keys — are masked in every
file, source code included. The name-driven pass (`NAME=value`, `"name": value`,
`Authorization:` headers, DSN and URL passwords) is the only one that catches a
shapeless secret such as `aws_secret_access_key`, and it runs everywhere
**except** source code, where `api_key=self._api_key` is an identifier and
`apiKey: NotRequired[str]` a type: masking those would hand back code that no
longer matches the file. Configuration data — `.env`, `~/.aws/credentials`,
`~/.kube/config`, `.netrc`, `.ini`, JSON and YAML — gets the full pass.
`grep_search` judges each matched line by the path it came from, so one search
can span both kinds of file.

`git_status`, `git_diff`, `git_log` and `git_commit` are scanned too, and always
with the full name-driven pass. `git_diff` hands back whatever the repository
happens to contain, a committed `.env` included, so the git arguments say
nothing useful about what kind of content is coming back. A credential on a
diff's `+`/`-` line — or on the `++`/`--` columns of the combined diff a
conflicted merge produces — is masked like any other, and with the same
non-reusable sentinel the file surfaces use, so a masked diff fed back through
`git apply` fails loudly instead of writing a dead-looking key.

### What the outbound guard refuses

`http_request`, `exec_command` and `execute_code` refuse to put credential
*material* on the network: a PEM private key, an `/etc/passwd`-shaped line, a
vendor-prefixed provider key (`sk-ant-…`, `ghp_…`, `AKIA…`), or a connection
string with an inline password. The shell check only applies to commands that
can actually reach the network.

An opaque API key in an `x-api-key` or `Authorization` header is **not**
refused — that is how authenticated APIs work, and it cannot be told apart from
exfiltration by looking at the bytes. What keeps it safe is the credential path
above, not a pattern match. `web_search` is stricter than the rest, because a
search provider has no business with any credential in a query.

Set `AGENTOS_SENSITIVE_PAYLOAD_DISABLED=1` before starting AgentOS to disable
the check entirely.

The gate applies on *write* only. Values you set in your shell or by editing
`~/.agentos/.env` by hand keep working exactly as before.

### Credentials that already exist elsewhere

Before telling you a variable is missing, AgentOS checks whether it is already
obtainable. Today that means the GitHub CLI: if `gh auth login` has been run,
`GITHUB_TOKEN` and `GH_TOKEN` are reported as available and can be imported
with one command or one click.

Two properties are deliberate. Checking never reads the credential — it runs
`gh auth status`, not `gh auth token` — so a listing that mentions a source has
not touched a secret. And importing only ever happens when you ask for it,
because a token you granted to another tool becoming available to an agent
should not be something that happens quietly.

An imported value is a copy. It stays as it was when imported and will not
follow the source's own rotation; re-import to refresh it.

### Skill settings are not secrets

A skill may also declare ordinary settings — a directory, a format, a default —
under `metadata.agentos.config` in its `SKILL.md`:

```yaml
metadata:
  agentos:
    config:
      - key: wiki.path
        description: Path to the knowledge base directory
        default: "~/wiki"
```

These live in the TOML config under `[skills.config]`, not in `.env`, because
there is nothing to hide and a visible, diffable setting is easier to share and
review:

```sh
agentos config set skills.config.wiki.path /srv/wiki --config ~/.agentos/config.toml
```

When the agent opens the skill, the values currently in effect are appended to
what it reads, so it does not have to ask or go looking.

## Skill Prompt Budget

Every eligible skill is listed for the agent in a block appended to the system
prompt. `[skills].max_skills_prompt_chars` caps how large that block may get:

```toml
[skills]
max_skills_prompt_chars = 28000
```

The budget degrades one step at a time, so overrunning it by one skill costs a
little description text rather than every description in the block:

| Step | `skills_render_mode` | What the agent sees |
| --- | --- | --- |
| Fits | `full` | Every skill, full description |
| Over | `full_truncated` | Every skill, descriptions shortened to the longest length that fits (floor 120 chars) |
| Over even at 120 | `compact` | Names only, plus a pointer to `skill_list` |
| Still over | `compact_truncated` | Fewer names |

The shortened length is the widest one that fits, not a fixed step, so the block
spends the budget you gave it rather than settling well under it. The agent is
told the descriptions are shortened and where to read the full text.

Only the last step drops skills, and it drops the lowest-precedence layer first
— `extra` (the directories you listed in `skills.extra_dirs`), then `bundled`,
and only then `managed`, `personal`, `project`, `workspace`. A drop is logged as
`skills_filter.budget_truncated` with the names, and the affected skills report
a `prompt_budget` reason on the Skills screen. Any step below `full` is logged
as `skills_filter.budget_degraded` and recorded per turn as
`skills_render_mode` in the decision log, because a block that quietly lost its
descriptions used to be indistinguishable from one with fewer skills in it.

The default of 24000 is sized so a default install lists every bundled skill
*with* its description and still has room for roughly 17 skills you add — past
that, descriptions start being shortened. If `skills_render_mode` is not `full`
and you have context to spare, raise the budget; the alternative is
`skills.filter_enabled`, which injects only the skills relevant to each message
and so needs far less room. Lower it only if you are running a model with a
small context window: the whole-request ceiling on a model below roughly 64k
tokens can be smaller than this budget, in which case the skills block alone
would not fit.

With `filter_enabled = false` the list is identical on every turn, so it is
injected as part of the cacheable system prompt. With filtering on it is re-picked
per message and is kept out of the cached prefix instead.

## Skill Install Policy

`[skills.install_policy]` runs an operator-owned command against every hub
skill before it is installed or updated:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turn the policy on. Enabled with no usable `command`, every install is refused. |
| `command` | `""` | Absolute path to the executable. Not resolved through `PATH`. |
| `args` | `[]` | Arguments, e.g. `["openclaw-install-policy"]` for ClawScan. |
| `pass_env` | `["PATH"]` | The only environment variables the command inherits. |
| `timeout_seconds` | `300` | The install is blocked when the command runs longer. |
| `max_output_bytes` | `1048576` | The install is blocked when the command prints more. |

The request and response format, and what `--force` can and cannot override,
are in [Install Security](features/skills.md#install-security).

## Skill Read Ceiling

`[skills].max_skill_view_chars` caps one `skill_view` result:

```toml
[skills]
max_skill_view_chars = 10000
```

This is a different budget from the one above, because a tool result is not
cached the way the system prompt is: a large skill costs its tokens again on
every re-read. The shipped skills are small (median 2 400 characters, largest
21 600), so the ceiling only engages for skills installed from a hub or written
for another agent — 56 000 characters for one, 87 000 for another, which is
14 000 to 22 000 tokens in a single tool result.

Over the ceiling, `skill_view` returns the skill's opening sections plus an
index of the rest, and the agent reads on with
`skill_view(name, section="<title>")` or `skill_view(name, file_path="...")`.
Two cases are deliberately left whole: a body with no headings, because there
would be no way to ask for the rest, and a body only slightly over the ceiling,
where the index would cost more than it saves. Set `0` to switch it off.

```sh
agentos config get skills.max_skills_prompt_chars
agentos config set skills.max_skills_prompt_chars 32000
agentos gateway restart
```

### Pinning a section

Which sections survive the cut is otherwise decided by where they sit in the
file, so a rule written into a large skill's tail is invisible and nothing says
so. A skill can mark a section to be returned wherever it sits, by writing
`<!-- always -->` on the line directly above its heading:

```markdown
<!-- always -->
## Rules

Every file this job needs lives in its own directory.
```

It is an HTML comment, so it renders as nothing anywhere the file is read as
markdown, and it only counts as a marker when it is the nearest non-blank line
above the heading — a skill that documents the marker does not pin whatever
heading follows the explanation. Pinned sections come out of the same ceiling
rather than adding to it: together they may take at most half of it, the opening
gets what is left, and a section that does not fit is left in the index instead
of being cut in half. Reserve it for invariants; pinning reference material
spends the opening on it.

A pinned section is worth little if it only lasts the turn that read it, so the
transcript keeps a `skill_view` result up to this ceiling rather than the short
preview it keeps of ordinary tool output. Raising `max_skill_view_chars` raises
what is stored per view with it.

## First-Run Wizard

```sh
agentos onboard
```

Common options:

```sh
agentos onboard --if-needed
agentos onboard --minimal
agentos onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
agentos onboard --provider openai --model gpt-5.4-mini --api-key-env OPENAI_API_KEY
agentos onboard --provider ollama --model llama3.1
agentos onboard status
```

The router mode defaults to `recommended`. Use `--router disabled` when you want
direct single-model routing.

## Reconfigure One Section

The `configure` command edits a selected section:

```sh
agentos configure provider --provider openrouter --api-key-env OPENROUTER_API_KEY
agentos configure router --router recommended
agentos configure router --router openrouter-mix
agentos configure router --router disabled
agentos configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
agentos configure x-search --api-key-env XAI_API_KEY
agentos configure channels
agentos configure image --image-provider openai --primary openai/gpt-image-1 --api-key-env OPENAI_API_KEY
agentos configure memory --memory-provider local --onnx-dir ~/.agentos/models/embeddings/google-embeddinggemma-300m
```

Supported sections:

- `provider`
- `router`
- `channels`
- `search`
- `x-search`
- `image-generation`
- `memory-embedding`

## Configuration Decision Table

| Need | Preferred command |
| --- | --- |
| First setup | `agentos onboard` |
| CI or install scripts | `agentos onboard --if-needed` |
| Change provider | `agentos configure provider ...` |
| Enable or disable routing | `agentos configure router ...` |
| Configure web search | `agentos configure search ...` |
| Configure messaging platforms | `agentos configure channels` |
| Inspect current values | `agentos config get` |
| Persist an advanced key | `agentos config set <key> <value> --config <path>` |
| Delegate coding to Claude Code / Codex | `agentos acp doctor`, then `[acp]` (see below) |

## Provider Configuration

Inspect provider support:

```sh
agentos providers list
agentos providers configure openrouter
agentos providers status
```

Onboarding-verified providers include:

- OpenRouter
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

AgentOS also carries provider registry entries for additional
OpenAI-compatible or self-hosted backends. Use `agentos providers list` on
your install to see the current catalog.

Read: [`providers-and-models.md`](providers-and-models.md)

### OpenCAP

See [Providers and Models — OpenCAP routing](providers-and-models.md#opencap-routing)
for the canonical setup, model catalog, routing, and pricing behavior.

### Surplus Intelligence

See [Providers and Models — Surplus Intelligence routing](providers-and-models.md#surplus-intelligence-routing)
for the canonical setup, model catalog, routing, and pricing behavior. Set
`AGENTOS_SURPLUS_LIVE_PRICING=0` to use static price estimates instead of its
live marketplace catalog.

### Ollama plain-text mode

AgentOS supports Ollama native tool calls. For a local model that does not
reliably implement Ollama's tool-call protocol, select the model separately,
then turn on plain-text mode.

Check which models are installed with `ollama list`, then configure the
provider and model:

```sh
agentos configure provider --provider ollama --model <your-local-model>
```

Then disable model-visible tools and the router in `agentos.toml`:

```toml
[tools]
enabled = false

[agentos_router]
enabled = false
```

For a remote or non-default host, set `base_url` under `[llm]` (e.g.
`http://10.0.0.42:11434`).

`tools.enabled = false` is a hard plain-text mode: no tool definitions are sent
to the provider and no tool handler is exposed for the turn. When you do enable
tools on smaller local models, keep a positive `agent_max_iterations` so
malformed or repetitive tool calls terminate predictably.

### Deferred tools

By default a request carries only the tools a typical turn needs. The long tail
— 25 builtins: audio generation, project and skill administration, `browser`,
`x_search`, `http_request`, `subagents`, and a few more — is left out; the system prompt
lists each by name with a one-line summary under "Tools that must be loaded
first", and so does the `tool_search` tool. The model calls `tool_search` to
load the ones a task needs, and they stay loaded for the rest of the session (a
reset starts lean again). On a stock gateway a request carries 40 tool schemas
instead of 64. The spreadsheet/document builders and the git tools stay loaded:
with them one load away the model reached for the shell instead. Reading a skill with `skill_view` also loads the tools that skill
lists in `requires_tools`. Policy still applies: a tool the session may not use
is neither listed nor loadable. A surface narrowed by an allowlist (an agent's
`tools.allow` or `profile`, a cron job's tools) defers nothing.

To send every tool on every request, as before:

```toml
[tools]
defer_loading = false
```

`agentos context` shows what a request carries with the current setting.

### Parallel tool calls

When a model has several tool calls that do not depend on each other, issuing
them in one response saves a full provider round trip per extra call; the
runtime already runs them concurrently. By default AgentOS asks for this with
`parallel_tool_calls: true` on the providers whose API is known to accept the
field (api.openai.com and OpenRouter); every other OpenAI-compatible endpoint
(OpenCAP, Bankr, Surplus, DeepSeek, Zhipu, Gemini-compatible, Ollama, local
servers) gets nothing, and Anthropic already allows parallel tool use unless
told otherwise. The system prompt's tool section carries the matching
instruction. To send nothing, as before:

```toml
[tools]
parallel_calls = false
```

### Loop guard

A model can get stuck calling the same tool with the same arguments and getting
the same answer back, or going back and forth between two calls. Each of those
calls succeeds, so nothing else stops the turn until its iteration budget runs
out. The loop guard watches every turn for that and acts in two steps:

1. **Warn.** Once a call has returned the same result three times, or two calls
   have alternated six times, a short `[Loop check]` note is appended to that
   tool result so the model reads it next to the output.
2. **Block.** A call that has already returned the same result four times (or
   that would stretch an alternation with no new results to eight calls) is not
   run: every call of that batch gets a result saying it was not run and why,
   and the model is asked to reassess. If the model proposes a repeat again in
   the same turn, the turn ends with the error code `loop_detected` and a
   message for the user.

Only repeats that produce nothing new count: a call whose result changed (a log
that grew, a file that was edited) starts over, and a successful file edit or
command resets the count of every other call. Status polls (`process` poll/log,
`*_status` tools, child-session status) get a much larger allowance before they
are refused. The guard starts from zero on each new message, so asking for the
same action again is never blocked by an earlier turn; the "same call failed
again" streak (`tool_failure_loop_block_threshold`) resets on a new message too.

To keep the old behaviour — decisions are only written to the turn call log:

```toml
[tools]
loop_guard = "observe"   # default "enforce"
```

### Repetitive output guard

A response that degenerates into the same line, paragraph or phrase over and
over is cut after the first repeated copy, its provider stream is closed, and
it is continued once; a second loop ends the turn with `repetition_detected`.
On by default with conservative thresholds; see
[Providers and Models — Repetitive Output Guard](providers-and-models.md#repetitive-output-guard)
for how it decides. To turn it off:

```toml
[llm.repetition_guard]
enabled = false
```

## Prompt Cache Configuration

Controls prompt prefix caching for LLM providers that support it (Anthropic, OpenAI, DeepSeek, OpenRouter, etc.):

```toml
[prompt_cache]
mode = "auto"   # "auto" | "on" | "off"
```

- `auto` (default): Enables prompt caching when the active provider and model support prefix caching.
- `on`: Forces prompt caching on.
- `off`: Disables prompt caching.

Environment variable override:
```sh
export AGENTOS_CACHE_MODE="auto"   # auto | on | off
```

> [!NOTE]
> The legacy `prompt_cache.enabled` key and `AGENTOS_CACHE_ENABLED` environment variable are deprecated and mapped automatically to `mode` (`on`/`off`) with a deprecation warning.

## Router Configuration

Router modes:

| Mode | Use when |
| --- | --- |
| `recommended` | You want the selected provider's default routing profile. |
| `openrouter-mix` | You want OpenRouter mixed-model defaults. |
| `disabled` | You want one configured provider/model for every turn. |

Commands:

```sh
agentos configure router --router recommended
agentos configure router --router openrouter-mix
agentos configure router --router disabled
```

Router-supported provider profiles depend on the installed build and configured
provider. Read [`features/agentos-router.md`](features/agentos-router.md) before
using direct model runs for evaluation. For what the four text tiers
(`c0`–`c3`) mean and how a turn is assigned to one, see
[Model Tiers](features/agentos-router.md#model-tiers).

### Router strategy

Independent of the mode above, `agentos_router.strategy` picks how the router
classifies each turn:

| Strategy | Default | Behavior |
| --- | --- | --- |
| `pilot-v1` | Yes | English-optimized local ML router: an AgentOS-native, self-trained model (MiniLM embeddings + ONNX inference). Decides on-device with no LLM call and nothing leaves the machine. The bundle ships in the wheel under `src/agentos/agentos_router/models/pilot_v1/`; when it's missing (e.g. a source checkout without `git lfs pull`) the strategy tags the decision `pilot_unavailable` and routes the turn to the default tier (c1). Runtime deps are `numpy`/`onnxruntime`/`tokenizers` (in the `recommended` and `ml-router` extras); a minimal install without them degrades the same graceful way. Tunable via the `[agentos_router.pilot]` sub-table below. See [`features/agentos-router.md`](features/agentos-router.md#the-pilot-strategy) for status and upgrade notes. |
| `llm_judge` | No | Each turn is classified by a small LLM judge call instead of the local ML bundle. See "Local judge" below. |
| `jev` | No (experimental) | Each turn is classified by one call to typesafe.ai's Jev "System One" classifier. Sends the current turn text to typesafe.ai; returns a calibrated probability that feeds the confidence gate plus a `high_risk` score that floors destructive / production requests at c3. Requires `TYPESAFE_API_KEY`; a missing key, HTTP error or timeout degrades to the default tier with `jev_unavailable` telemetry. Tunable via the `[agentos_router.jev]` sub-table below. See [`features/agentos-router.md`](features/agentos-router.md#the-jev-strategy). |

```toml
[agentos_router]
strategy = "pilot-v1"   # default; or "llm_judge", or "jev" (experimental)
```

### Effort by tier

A turn the router sends to a cheap tier also runs with less reasoning: when the
thinking level is the router's own pick (the shipped tier table's level, or
the pilot controller's per-turn choice), `c0` turns run at most at `low` and `c1` turns at most at `medium`; `c2`, `c3`
and the image tier keep their configured level. A level is only ever lowered,
never raised, and the model choice does not change. It does not apply when
`[llm] thinking` is set, when an explicit model overrode the route, when the user pinned a route or a model with
router control, or when `[agentos_router.tiers.<tier>] thinking_level` is set to
something other than the shipped value. Config files store the whole tier table,
so a level equal to the shipped one cannot be told apart from a hand-written
one; use `effort_by_tier = false` to keep it. Only providers with a thinking-level concept are affected
(Anthropic effort, OpenAI reasoning effort, OpenRouter `reasoning.effort`).
Each lowered turn logs `turn_runner.effort_by_tier`. To keep the tier's level
on every turn:

```toml
[agentos_router]
effort_by_tier = false
```

Jev settings live in their own sub-table (all fields optional; defaults shown):

```toml
[agentos_router.jev]
# api_key = "..."                   # literal key; prefer the env var below
api_key_env = "TYPESAFE_API_KEY"    # env var consulted when api_key is unset
base_url = "https://api.typesafe.ai"
model = "jev-latest"
input_max_chars = 4000              # min 1000; the turn is head/tail truncated
high_risk_threshold = 0.7           # 0..1; high_risk >= this floors the turn at c3
# timeout_seconds = 8.0             # unset: derived from routing_timeout_seconds
short_circuit_enabled = true        # greetings/acks skip the API call
agentic_floor_enabled = false       # true: tool-bearing turns never go below c1
```

`api_key` is redacted on every public surface (`agentos config get`, the Web
UI, RPC) and is **never written to `config.toml` when it equals
`$TYPESAFE_API_KEY`** — the wizard stores the key in the env store and leaves
the config pointing at `api_key_env`.

Router runtime dependencies (`onnxruntime`, `numpy`, `tokenizers`) stay in the
`recommended` / `ml-router` extras rather than the core install: a minimal
install without them does not fail — the router degrades to the default tier
and emits `pilot_unavailable` telemetry.

The supported strategies are also selectable from the Mode dropdown in
onboarding (Web UI wizard and CLI), a four-option selector: **Local ML —
English-optimized (Pilot)** (`pilot-v1`, the default), **Smart routing
(LLM-based)** (`llm_judge`), **Jev cloud classifier (typesafe.ai,
experimental)** (`jev`), or **Off**. The legacy **Smart routing (on-device)**
(`v4_phase3`) option is no longer offered. The "Judge model" field only appears
for the LLM-based strategy; the "Pilot safety net" field only appears for the
Pilot strategy; the "TypeSafe API key" field only appears for the Jev strategy.

#### Provider-switch profiles

When onboarding switches to another LLM provider, AgentOS saves a profile for
the provider being left and restores it when you return. Profiles live under
`[provider_profiles]` in `config.toml`.

A profile holds only what belongs to that provider:

- the active model and the non-secret connection settings — `base_url`,
  `proxy`, `api_key_env`, `max_tokens`, `thinking`, and provider routing
  preferences;
- the provider's router slice — whether the router is enabled, its tier
  profile, any tiers you authored yourself, and the Smart Routing judge target
  (`judge_model`, `judge_provider`, `judge_base_url`).

Install-wide router settings are deliberately **not** part of a profile:
`strategy`, `default_tier`, `rollout_phase`, `auto_thinking`, the Pilot
thresholds and the judge short-circuit tuning all stay on `[agentos_router]`.
Retuning any of them while another provider is active is kept, not reverted by
the next switch.

Tier tables that AgentOS wrote itself — the shipped per-provider defaults, or
the pinned tiers of a local provider — are not stored in the profile. They are
re-derived when you switch back, so upgrading AgentOS still moves you onto the
current recommended models. Tiers you edited yourself are stored verbatim and
restored unchanged.

Passing an explicit model when you switch back always wins over the remembered
one, and a local provider's tiers are re-pinned to it.

Credentials are never copied into a profile: neither a literal `api_key` nor a
local `judge_api_key`. Returning to a provider you configured with a literal
key asks you to re-enter it. Use `api_key_env` for credentials you want to
survive a provider switch.

A profile is only saved for a provider you actually configured, and a profile
that no longer parses is dropped on load rather than blocking gateway startup.

#### Upgrading from v4_phase3

Historical onboarding persisted `strategy = "v4_phase3"` explicitly in
`~/.agentos/config.toml`. The default has since flipped to `pilot-v1`, so on the
next config load AgentOS **automatically migrates** any config still pinning
`v4_phase3`:

- the strategy is rewritten to `pilot-v1`;
- the original file is backed up verbatim next to it as
  `config.toml.backup.<timestamp>` (mode `0600`);
- the flip is logged, and the rewrite is idempotent (a config already on
  `pilot-v1` is left untouched, with no backup).

There is no way to keep `v4_phase3` in config — the legacy engine and its
model bundle were removed from the tree (Phase C), onboarding no longer offers
it as a Mode option, and a value that bypasses the file migration (e.g. an env
override) normalizes to `pilot-v1` at config load.

#### Pilot strategy settings

When `strategy = "pilot-v1"`, the optional `[agentos_router.pilot]` sub-table
tunes the Pilot router:

```toml
[agentos_router]
strategy = "pilot-v1"

[agentos_router.pilot]
# Under-routing safety-net floor (0.0–1.0). The effective cutoff is
# max(safety_net_threshold, router.confidence_threshold), so a value below the
# confidence threshold has no effect. Default 0.5.
safety_net_threshold = 0.5

# Override the Pilot artifact directory. Defaults to the bundled
# `models/pilot_v1/` root; set this to point at a bundle elsewhere on disk.
# pilot_artifact_dir = "~/pilot_v1"
```

### Local judge (Ollama / LM Studio)

This section applies only when `agentos_router.strategy = "llm_judge"` — the
default `pilot-v1` local strategy makes no LLM call
and have no judge to configure.

With the `llm_judge` strategy, the router classifies each text turn with a
small LLM judge. Instead of a cloud model you can point the judge at a local
OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, vLLM) — no cloud
credentials required, and zero bytes added to the package.

In the interactive router setup, pick **Local endpoint** for the judge model and
supply the base URL (for example `http://localhost:11434/v1`) and the model
name. Onboarding validates the URL shape and runs one test classification call
to verify the endpoint is reachable.

Equivalent config (`[agentos_router]` in `agentos.toml`):

```toml
[agentos_router]
strategy    = "llm_judge"
judge_model = "llama3"
judge_base_url = "http://localhost:11434/v1"
# judge_api_key is optional — local endpoints usually accept any token; a
# placeholder is used when unset. It is redacted in logs.
judge_api_key = ""
```

`judge_base_url` only takes effect when `judge_model` is set. When it is, the
judge client is built against that endpoint and the usual "judge provider must
match `llm.provider`" constraint is bypassed. The resolved judge is logged at
boot (`router.judge_resolved` with `source="local"` and the base URL) and
reported by `agentos doctor`.

## Search Configuration

Inspect search providers:

```sh
agentos search list
agentos search status
agentos search query "AgentOS release notes"
```

Configure search:

```sh
agentos configure search --search-provider duckduckgo
agentos configure search --search-provider brave --api-key-env BRAVE_SEARCH_API_KEY
agentos configure search --search-provider tavily --api-key-env TAVILY_API_KEY
```

Runtime-supported search providers in this build include Brave Search, DuckDuckGo,
and Tavily Search. Additional provider metadata may be present for future or
not-yet-runtime-supported integrations.

Read: [`search.md`](search.md)

## X (Twitter) Search Configuration

`x_search` is a separate tool, not a `web_search` backend: xAI runs the search
against X's post index and returns a synthesized answer with citations.

Credentials come from either a SuperGrok / X Premium+ login or an xAI API key,
and the login wins when both exist:

```sh
agentos auth login xai
agentos auth status
```

```sh
agentos onboard catalog x-search
agentos configure x-search --api-key-env XAI_API_KEY
agentos configure x-search --x-search-model grok-4.5
```

| Key | Default | Notes |
| --- | --- | --- |
| `x_search.enabled` | `true` | Off hides the tool even when a key is present. |
| `x_search.model` | `grok-4.5` | Any Grok model with server-side `x_search` access. |
| `x_search.base_url` | `https://api.x.ai/v1` | Must be HTTPS; a rejected value falls back to the default. |
| `x_search.api_key` | `""` | Pasted key. Persisted to config like other capability keys. |
| `x_search.api_key_env` | `XAI_API_KEY` | Read at call time, so no restart after changing the variable. |
| `x_search.reasoning_effort` | `""` | `low`, `medium`, `high`, `xhigh`, or empty for the model default. |
| `x_search.timeout_seconds` | `180.0` | One attempt. Range 30-300. |
| `x_search.total_timeout_seconds` | `300.0` | Whole call including retries. Range 30-600. |
| `x_search.retries` | `2` | 5xx, timeout, and connection errors only. Range 0-5. |

The tool is hidden from the model until a credential resolves, and its usage
bills xAI directly rather than appearing in `agentos cost`.

Read: [`x-search.md`](x-search.md)

## Browser automation

```toml
[browser]
enabled = true
headless = true
cdp_port = 0
attach_confirmed = false
allowed_domains = []
restrict_evaluate = false
```

| Key | Default | Notes |
| --- | --- | --- |
| `browser.enabled` | `true` | Off hides the tool even when the binary is installed. |
| `browser.headless` | `true` | Managed mode; `false` opens a visible window. |
| `browser.binary_path` | `""` | Optional explicit path to `agent-browser`; otherwise found on `PATH`, then in the usual npm global directories (nvm, fnm, Volta, pnpm, Bun, Homebrew, `~/.npm-global`). |
| `browser.cdp_port` | `0` | `0` = managed. `>0` = attach to your Chrome's debug port. Localhost only; a URL is never accepted. |
| `browser.attach_confirmed` | `false` | Must be `true` for attach mode to run — it can drive signed-in sessions. |
| `browser.allowed_domains` | `[]` | `[]` = open web (SSRF still blocks private ranges). A non-empty list bounds navigation in AgentOS and in the engine. Entries are hostnames and cover subdomains — `["example.com"]` allows `example.com` and `www.example.com`, but not `evil-example.com`. A leading `.` or `*.`, a scheme, a port, userinfo and a path are accepted and reduced to the hostname, so `https://example.com/x` and `*.example.com` mean the same as `example.com`. An entry that cannot be reduced to a hostname is refused at config time, naming the accepted format. |
| `browser.persist_profile` | `false` | `true` keeps cookies/login between sessions (written to disk). |
| `browser.session_ttl_minutes` | `15` | Idle sessions are reaped after this. Range 1-1440. |
| `browser.max_sessions` | `3` | Concurrent browser sessions; oldest-idle evicted. Range 1-20. |
| `browser.snapshot_max_chars` | `24000` | Snapshots over this are truncated with a marker. |
| `browser.dialog_policy` | `must_respond` | `must_respond`, `auto_dismiss`, or `auto_accept` for native dialogs. |
| `browser.dialog_timeout_s` | `300.0` | Watchdog for an unanswered dialog. Range 1-3600. |
| `browser.restrict_evaluate` | `false` | `true` blocks sensitive JS primitives in `eval` (off by default — it also blocks ordinary DOM extraction). |
| `browser.allow_unsafe_evaluate` | `false` | `true` overrides `restrict_evaluate` for a trusted page. |

The tool is hidden from the model until the `agent-browser` binary is installed.

Read: [`features/browser.md`](features/browser.md)

## Channel Configuration

List supported channel types:

```sh
agentos channels types --json
agentos channels describe slack
agentos channels add telegram --name personal
agentos channels status
```

Channel saves update configuration. Restart the gateway after edits:

```sh
agentos gateway restart
agentos channels status <name> --json
```

See [`channels.md`](channels.md) for details.

## MCP Configuration

Use **Settings > MCP Servers** in the Web UI for normal MCP setup. It supports
live connect/disconnect controls, inline validation, OAuth authorization, and a
featured Robinhood Trading preset.

For scripted deployments, configure servers in TOML:

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

Supported transports are `stdio`, `sse`, and `streamable_http`. The MCP SDK is
included in the standard AgentOS installation, so Streamable HTTP and OAuth work
without installing an additional package extra.

`stdio` servers are spoken to with the framing the MCP stdio transport defines:
one compact JSON-RPC message per line on the subprocess's stdin and stdout.
AgentOS neither sends nor expects the LSP-style `Content-Length` header, so the
reference `@modelcontextprotocol/server-*` servers work unmodified. Replies are
matched to their request id, so notifications a server interleaves with them are
skipped rather than mistaken for a result.

`sse` is the legacy HTTP+SSE transport from the 2024-11-05 spec revision; prefer
`streamable_http` for new servers. On `sse`, `url` is the endpoint AgentOS opens
the event stream against — the URI it POSTs JSON-RPC to is chosen by the server
and announced in the stream's `endpoint` event, so there is nothing to configure
for it. A relative endpoint resolves against `url`; one pointing at a different
scheme or host is refused and nothing is posted to it. If the server opens the
stream but never advertises an endpoint, the connection fails after
`tool_timeout_seconds`.

The HTTP transports accept `http://` and `https://` URLs only, and both connect
through the same SSRF guard the built-in HTTP tools use. `http://localhost:PORT`
and LAN-hosted servers keep working — the guard blocks cloud metadata endpoints
(`169.254.169.254` and friends), which serve instance credentials and are never
a valid MCP target. The check runs against the address the socket actually
dials, so a short-TTL DNS name cannot answer safely for the check and with the
metadata address for the connection.

### MCP tool names and results

Each tool a server exposes is registered for the agent as `mcp_<tool>`, so a
server's `search` tool is `mcp_search`. That short name is what tool
allowlists and denylists refer to.

When two servers expose a tool with the same name, the first one to connect
keeps the short name and the later one is registered as
`mcp_<server>_<tool>` instead of replacing it. Servers connect at startup in
the order they appear in `[[mcp.servers]]`, so the first configured server
wins. `<server>` is the server's `name` lowercased with every run of characters
outside `[a-z0-9_]` turned into `_` (`Linear-Prod.v2` becomes
`linear_prod_v2`); if two server names reduce to the same text, a `_2`, `_3`
suffix keeps them apart. The gateway logs `mcp.tool_name_collision` naming the
server that got the long name, and the `mcp.status` RPC lists each connected
server's `tools` plus a `toolMap` from registered name to the server's own tool
name. A server connected later (after OAuth, or from the MCP screen) takes
whichever names are free at that moment.

When a server marks a result as an error (`isError`), the call comes back to
the model as a failed tool result whose message quotes the server's error text
(capped). A call that exceeds `tool_timeout_seconds` fails the same way.

Output from an MCP tool is written by the server, not by AgentOS, so it is
handled like `web_fetch` output: wrapped in an `<untrusted source='mcp:<server>/<tool>'>`
envelope and counted against the external-content result budget.

OAuth access and refresh tokens are not written to `config.toml`. AgentOS keeps
them in a server-scoped JSON file under the configured state directory with
file mode `0600` inside a `0700` directory on POSIX systems. On Windows, the
credential file inherits the current user's state-directory ACL. Removing the
server from the MCP screen also clears that credential file.

## Memory Configuration

Useful commands:

```sh
agentos memory status
agentos memory index
agentos memory list
agentos memory search "project preference"
agentos memory show <path>
```

Configure embedding behavior:

```sh
agentos configure memory-embedding
```

Memory can combine Markdown-backed sources with SQLite keyword and semantic
indexes. The exact memory shape depends on the configured provider and local
embedding support.

An external memory provider (`[memory.provider]`) is asked for recall before
every turn. `[memory] recall_timeout_seconds` (default `1.5`) is a hard
deadline on that recall: a slower recall does not hold the turn, which goes on
without it and logs `memory.recall_timeout`. The late recall is not cancelled;
it finishes in the background and its result stands in on the session's next
turn if that turn's recall is late too.

```toml
[memory]
recall_timeout_seconds = 1.5
```

### Turn setup concurrency

Before a turn's first model request AgentOS reads several independent things:
the memory-provider recall, the session's project knowledge, the deferred tools
the session has loaded, the recent transcript for the router, and on the first
turn of a session the memory index warm-up. `[task_runtime]
concurrent_turn_setup` (default `true`) runs them concurrently and applies the
results in the same order as before, so the prompt is byte-identical either
way. Set it to `false` to go back to running them one after another.

```toml
[task_runtime]
concurrent_turn_setup = true
```

### Interrupted turns

A turn that a gateway restart or crash cut short is resumed on the next boot
from what its transcript shows; one whose last tool calls have no recorded
result resumes with read and status tools only. `[task_runtime]
resume_interrupted_turns` (default `true`) controls this; `false` only reports
interrupted turns (a note in the session, `/ready` `turn_recovery`,
`agentos doctor`). See [Gateway: interrupted turns](gateway.md#interrupted-turns).

```toml
[task_runtime]
resume_interrupted_turns = true
```

Read: [`features/memory.md`](features/memory.md)

## Context Compaction

When a session's request nears the model's context window, the oldest history
is folded into one summary exchange. The trigger is `preflight_compact_ratio`
(default 0.85) of the context window, or the provider's request budget when
that is tighter.

```toml
[compaction]
summary_timeout_seconds = 45.0   # bound on the summary call (auxiliary task "compaction")
proactive = true                 # compact between turns, in the background
proactive_ratio = 0.70           # ...once the history reaches 70% of the trigger
proactive_idle_seconds = 20      # ...after the session has been idle this long
estimate_calibration = true      # scale the size estimate by what providers report
```

- **Proactive compaction.** After a turn ends with the history at
  `proactive_ratio` of the trigger or more, AgentOS compacts the session in the
  background, so the next turn does not wait for the summarizer. The job waits
  until the session has been idle for `proactive_idle_seconds` (default 20,
  0 = at once) before it calls the summarizer, so a quick follow-up does not
  pay for a summary it throws away. A turn that starts first wins: the
  background result is dropped and the check runs again when that turn ends.
  When a turn cancels a summary already in progress, the wait doubles for that
  session (up to 300 s) until a job completes. A job that has already
  persisted is never cancelled; one whose session was reset or truncated
  meanwhile is skipped quietly. Subagent and cron sessions are never compacted this
  way. The in-turn check stays as the safety net. `proactive = false` turns
  the background job off.
- **Estimate calibration.** The request size is estimated as characters / 4,
  which over-counts against what providers bill. Per provider and model,
  AgentOS keeps a running ratio of the provider's reported input tokens to its
  own estimate and, after two calls, scales the estimate by it (clamped to
  0.7-1.25), so compaction does not fire early. Usage the provider marks as
  estimated and requests carrying images or documents are not counted. `estimate_calibration = false`
  uses the raw estimate.
- **Summary format.** The summary has fixed sections: Goal, Constraints and
  preferences, Done, In progress, Blocked, Key decisions, Next steps, and
  Critical context (exact paths, commands, numbers and IDs). A later
  compaction hands the previous summary to the summarizer as the base to
  revise, so finished work stays under Done instead of being summarized away.
  Below the model's text AgentOS appends the files the compacted tool calls
  read (`read_file`, `read_spreadsheet`, `pdf`) and modified (`write_file`,
  `edit_file`, `apply_patch`), merged with the previous summary's lists and
  capped to the newest 20 of each. Shell commands are not parsed for paths.
  The in-turn, the proactive and the manual (`/compact`, `sessions.compact`)
  compaction all write this format. A manual compaction revises the newest
  stored summary, and from then on only the revision is replayed, not the two
  side by side; if its summarizer call fails, the earlier summary is kept and
  replays on its own. Summaries stored before this behaviour existed replay
  as they always did.
- When the summarizer fails or times out, a deterministic digest bounded to an
  eighth of the context window is used instead, so a compaction still shrinks
  the history.

## Auxiliary Model

Not every provider call belongs to a turn. Analysing a document the user
attached and describing an image are work AgentOS initiates on its own behalf.
These run against the `[auxiliary]` model rather than the agent's:

```toml
[auxiliary]
provider = ""            # empty = reuse [llm]
model = ""               # empty = reuse [llm]
timeout_seconds = 120.0

[auxiliary.tasks.vision]
model = "openai/gpt-4o-mini"
```

Point it at something cheap when these tasks do not need your main model. Tasks
currently in use are `document`, `vision`, `session_title`, `suggest_next` and
`compaction` (the in-turn context summary; it defaults to the router's cheapest
text tier and is bounded by `[compaction] summary_timeout_seconds`); a task with a
capability requirement wants its own entry, since a text-only model cannot
describe an image.

Resolution runs highest-first: `AGENTOS_<TASK>_MODEL` (for example
`AGENTOS_VISION_MODEL`), then `[auxiliary.tasks.<task>]`, then a
capability-aware default such as the router's image-capable tier, then
`[auxiliary]`, then `AGENTOS_LLM_MODEL`, then `[llm]`. An install that sets
none of this keeps using the `[llm]` model exactly as before.

These calls are billed to the session that triggered them and are additionally
tracked under an `aux:<task>` scope, so `agentos cost` can separate what the
agent spent answering from what the runtime spent on its own.

### Session titles

A new chat session is named from its first message — whether it started in the
web console, the desktop app or a channel such as Telegram: the `session_title` task
asks the auxiliary model for a 3–6 word title in the user's language, and the
result lands in the session's `display_name`. With no task override it prefers
the router's cheapest text tier (`c0`, else `c1`) so titling never waits on a
slow thinking model, where the web console, the
desktop app and `agentos sessions` all read it. It only names sessions that
still carry a placeholder (`WebChat` or the short id); a name you typed, or one
the agent set with `session_rename`, is never overwritten. The title is
generated off the turn path, so sending is never slowed down, and open clients
receive a `sessions.changed` event with `reason = "renamed"` when it lands.

```toml
[sessions]
auto_title = true                 # AGENTOS_SESSIONS_AUTO_TITLE=false to disable
auto_title_timeout_seconds = 30.0

[auxiliary.tasks.session_title]
model = "openai/gpt-4o-mini"      # or AGENTOS_SESSION_TITLE_MODEL
```

If the model is unreachable the first clause of the message is used instead,
so a session still gets a readable name.

### Suggested next prompt

When a turn finishes, the chat input in the web console, the desktop app and
`agentos chat` shows the message you are most likely to send next as grey
placeholder text. `Tab` takes it into the input and `Enter` sends it; typing
anything else ignores it. Clients ask for it with the `chat.suggestNext` RPC
once they see the turn end, and the `suggest_next` task answers from the last
user message and reply, preferring the router's cheapest text tier like
session titles do. A turn that errored or was aborted gets no suggestion and
costs no call, and the answer is cached per reply, so several clients open on
one session share a single call. There is no fallback: if the model is
unreachable or has nothing useful to offer, the normal placeholder stays.

```toml
[sessions]
suggest_next = true                  # AGENTOS_SESSIONS_SUGGEST_NEXT=false to disable
suggest_next_timeout_seconds = 15.0

[auxiliary.tasks.suggest_next]
model = "openai/gpt-4o-mini"         # or AGENTOS_SUGGEST_NEXT_MODEL
```

## ACP Coding Harnesses

`[acp]` controls how AgentOS drives external coding harnesses (Claude Code,
Codex, Gemini CLI, Cursor, ...) over the Agent Client Protocol. The defaults
work without any `[acp]` table; nothing starts until the agent spawns an ACP
session or you run `agentos acp run`.

```toml
[acp]
enabled = true
default_agent = "claude"
allowed_agents = ["*"]
permission_mode = "ask"
non_interactive_permissions = "fail"
approval_timeout_s = 900
turn_timeout_s = 0
max_concurrent = 8
# state_dir = "~/.agentos/state/acp"
isolate_codex_home = true
install_missing_adapters = true
stream_delivery = "live"
allowed_cwds = []

[acp.agents.cursor]
command = "agent"
args = ["acp"]
env = {}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | `false` refuses ACP spawns and `agentos acp run`. |
| `default_agent` | `"claude"` | Harness used when `sessions_spawn(runtime="acp")` names none. |
| `allowed_agents` | `["*"]` | Harness ids the agent may launch; `*` = any. |
| `permission_mode` | `"ask"` | Who answers harness permission prompts: `ask` (approval queue), `approve-reads`, `approve-all`, `deny-all`. |
| `non_interactive_permissions` | `"fail"` | `ask` with nobody to answer: `fail` the turn or `deny` the request. |
| `approval_timeout_s` | `900` | Seconds an `ask` prompt waits before it is rejected. |
| `turn_timeout_s` | `0` | Wall-clock limit per harness turn; `0` = none. |
| `max_concurrent` | `8` | Live harness sessions per gateway (1-64). |
| `state_dir` | unset | Overrides `<state>/acp` (session records, leases, logs, Codex home, worktrees). |
| `isolate_codex_home` | `true` | Give Codex its own `CODEX_HOME` with your login copied in and the session directory trusted. |
| `install_missing_adapters` | `true` | Allow the `npx -y` / `uvx` adapter download on first use. |
| `stream_delivery` | `"live"` | `live` streams the harness's text into the child session as it arrives; `final_only` sends the answer once at the end. |
| `agents.<id>.command` / `args` | unset | Replace the built-in launch command for that harness, or add a new harness id. |
| `agents.<id>.env` | `{}` | Extra environment for that harness only. |
| `allowed_cwds` | `[]` | Extra directories a harness may use as `cwd`; otherwise `cwd` must be inside the agent workspace or a managed worktree. The workspace suits read-only work in a read-only `harness_mode`; list a project here for a harness that writes (`agentos config set acp.allowed_cwds '["~/Code/my-app"]'`, which replaces the whole list). |

```sh
agentos config set acp.permission_mode approve-reads
agentos config set acp.default_agent codex
agentos acp doctor
```

Changes apply after a gateway restart. Read: [`acp-agents.md`](acp-agents.md)

## Sandbox and Permissions

Inspect or change posture:

```sh
agentos sandbox status
agentos sandbox on
agentos sandbox full
agentos sandbox bypass
agentos sandbox reset
```

Single-shot automation permissions:

```sh
agentos agent --permissions restricted -m "Read the repo and summarize it"
agentos agent --permissions full -m "Make a local patch and run tests"
```

For unattended automation that must stay inside a workspace:

```sh
agentos agent \
  --workspace /path/to/project \
  --workspace-lockdown \
  --scratch-dir /path/to/project/.scratch \
  -m "Investigate and propose the smallest fix"
```

Read: [`tools-and-sandbox.md`](tools-and-sandbox.md)

## Safety Configuration

Controls prompt-ingress safety scanning and untrusted workspace containment:

```toml
[safety]
wrap_untrusted_workspace = true
injection_scan_mode = "report"   # "report" | "enforce" | "off"
```

- `wrap_untrusted_workspace` (default `true`): Wraps files read from untrusted workspace directories with safety bounding markers to mitigate prompt-injection framing in workspace files.
- `injection_scan_mode` (applied to bootstrap workspace files, not all ingress):
  - `report` (default): Scans those files and logs detected patterns without changing the content; the turn still runs.
  - `enforce`: Redacts matched content from untrusted workspace files before it reaches the prompt; the turn still runs.
  - `off`: Disables prompt-injection scanning.

`[safety]` is TOML-only; there is no environment-variable override for these keys.

## Spend Budgets

Money ceilings with a hard stop, so a runaway loop or subagent fan-out cannot
burn an unbounded amount overnight. Every value is US dollars of estimated
model spend (or the provider-billed figure, when the provider reports one).

```toml
[budgets]
enabled = true          # master switch; keeps the numbers but suspends enforcement when false
session_limit = 5.0     # hard stop once one session reaches this
session_warn = 4.0      # one-shot warning for that session
daily_limit = 50.0      # hard stop for gateway-wide spend on the current UTC day
daily_warn = 40.0       # one-shot warning for that day
turn_reservation = 0.25 # headroom held for each in-flight turn (0 disables)

[budgets.agent_daily_limit]
main = 20.0

[budgets.channel_daily_limit]
telegram = 10.0
```

`agent_daily_warn` and `channel_daily_warn` take the same per-key shape as
their `*_limit` counterparts.

- Nothing is enforced by default: every ceiling is unset on a fresh install,
  and `enabled = true` only means "apply the ceilings you have configured".
- A turn that starts at or above a hard limit is refused before any provider
  call, with a `budget_exceeded` error naming the scope and the number. The
  refusal is recorded in the session transcript, and the user message that
  triggered it is rolled back rather than left as a reply-less turn.
- Ceilings are re-checked between iterations *within* a turn as well, so a
  single turn with a long tool loop stops at the ceiling instead of running
  to completion past it.
- A `*_warn` threshold does not stop the turn. It surfaces a
  `budget_warning` once per session (session scope) or once per UTC day
  (daily scopes), so a long session does not repeat the same alert every turn.
- A warn threshold above its own hard limit is rejected at load time — it
  could never fire. So are two spellings of one scope key (`default` and
  `main`, `Telegram` and `telegram`), which would otherwise silently drop one
  of the two numbers.
- Spend is tracked in a ledger persisted at
  `~/.agentos/state/spend_ledger.db` (keyed per UTC day for the daily scopes,
  per session for the session scope), so a ceiling survives a gateway restart
  — a runaway overnight loop cannot be reset by a crash-and-respawn.
- Subagent turns run through the same gate, and their spend is attributed to
  the parent agent id, so a fan-out is bounded by the agent and daily
  ceilings rather than multiplying past them.
- Admission *reserves* as well as checks. Spend is only recorded as a turn
  burns tokens, so children dispatched at the same moment would otherwise all
  read the same pre-fan-out total and all clear the same ceiling. Each admitted
  turn holds `turn_reservation` dollars (default `0.25`) against every scope
  its session bills to, and the hold is released the moment the turn ends —
  including on error, cancellation, and an abandoned turn. It therefore bites
  only within that much of a limit, and only on turns that overlap: a ceiling
  is never shrunk for turns that run one after another. Set
  `turn_reservation = 0` to reserve nothing. The re-check *inside* a turn
  weighs recorded spend only, so a turn is never stopped by its own
  reservation.
- Changing a ceiling requires a gateway restart to take effect. `budgets.*` is
  not on the live-reload allowlist, so both `agentos config` and the Web UI
  report `restart_required` when you change one. Restart before relying on a
  new ceiling — a limit written while the gateway is running is not yet
  enforcing.
- Enforcement fails open. If the ledger cannot be read or written, the
  shortfall is logged (`usage_tracker.ledger_*`) and turns keep running: a
  budget check is never the reason a gateway stops working. Reads reconcile
  the persisted row against in-process accounting by taking the larger of the
  two, so a dropped write cannot quietly retire a ceiling within a run.

`[budgets]` is TOML-only; there is no environment-variable override for these
keys.

## Gateway Binding

Foreground:

```sh
agentos gateway run --listen 127.0.0.1 --port 18791
```

Managed:

```sh
agentos gateway start --json
agentos gateway status   # shows both the CLI and running-gateway versions
agentos gateway stop
agentos gateway restart
```

`agentos gateway status` reports the installed CLI version and the running
gateway's version; when they differ it appends a mismatch line advising a
restart (typically after `agentos upgrade --no-restart` or a manual package
upgrade).

Bind precedence:

1. `--listen`
2. `--bind`
3. `--bind-mode`
4. `AGENTOS_LISTEN`
5. `AGENTOS_GATEWAY_HOST`
6. `[gateway] bind`
7. config host
8. `127.0.0.1`

```toml
[gateway]
bind = "loopback"          # loopback | lan | tailnet | custom; unset = use `host`
public_origin = ""         # e.g. "https://agent.example.com" for a public console
config_reload = true       # apply edits to this file without a restart
```

| Key | Default | Meaning |
| --- | --- | --- |
| `gateway.bind` | unset | `loopback` = `127.0.0.1`; `lan` = `0.0.0.0` (token auth required); `tailnet` = the `tailscale ip -4` address plus `127.0.0.1`, never a wildcard (startup fails when Tailscale is unavailable); `custom` = the `host` key. Unset keeps `host` in charge. `--listen` / `--bind` count as `custom`. Restart to apply. |
| `gateway.public_origin` | `""` | The browser origin a public deployment is opened at. Joins the origin allowlist and the `Host` guard. Required before the Control UI starts on a specific public address. Restart to apply. |
| `gateway.config_reload` | `true` | Watch the config file; see [Editing the file while the gateway runs](#editing-the-file-while-the-gateway-runs). |

Browser access follows one origin policy on every bind (allowlist, then
private same-origin, then a loopback origin on a local socket); see
[Gateway → Browser origins](gateway.md#browser-origins).
`cors.allowed_origins` entries join that allowlist; its old default `["*"]`
adds nothing, and CORS never answers `*`. Environment overrides:
`AGENTOS_GATEWAY_GATEWAY__BIND`, `AGENTOS_GATEWAY_GATEWAY__PUBLIC_ORIGIN`,
`AGENTOS_GATEWAY_GATEWAY__CONFIG_RELOAD`.

## Gateway Control

The gateway can restart itself in place on request (`/restart` in a chat, the
`gateway.restart` RPC, `agentos gateway restart`; see
[Gateway](gateway.md#when-to-restart)). Who may ask depends on the surface:

```toml
[gateway_control]
chat_restart = false   # /restart from paired channels and the agent's gateway tool
```

- Operators on a Control surface — the web console, the desktop app, the CLI —
  can always restart the gateway. They already hold `updates.apply` and
  `config.patch`, which are strictly more powerful.
- A paired channel sender (`/restart` on Telegram, Discord, Slack, …), a
  connection bound to an agent, and the agent's own `gateway` tool are refused
  unless `chat_restart` is `true`. Any paired sender may restart once it is;
  there is no owner or admin tier.

`chat_restart` defaults to `false`. Changing it requires one restart of the
gateway (`agentos gateway restart`, or `/restart` from the web console), after
which chat restarts are accepted. Environment override:
`AGENTOS_GATEWAY_GATEWAY_CONTROL__CHAT_RESTART=true`. In-place restart is not
available on Windows.

## Scheduler

```toml
[scheduler]
startup_agent_delay_seconds = 120   # 0 = run missed agent jobs at boot
```

Cron jobs missed while the gateway was down are caught up at boot (at most 5,
staggered over 10 seconds). Missed agent jobs — an agent turn, or a main-session
system event — wait `startup_agent_delay_seconds` first so they do not compete
with channel start-up for the model; reminders and script jobs run at once. The
wait is stored as the job's next run time, so `agentos cron list` shows it.
Restart the gateway after changing it. Environment override:
`AGENTOS_GATEWAY_SCHEDULER__STARTUP_AGENT_DELAY_SECONDS=300`. See
[Scheduling](scheduling.md#sleep-restarts-and-clock-changes).

## Update Notifications

On commands that connect to the gateway, the CLI checks PyPI at most once every
24h and, if a newer release of `use-agent-os` exists, prints a one-line notice
on stderr suggesting `agentos upgrade`. Similarly, the Web UI queries the gateway
on connection and displays a dismissible banner if an update is available.
The check is silent on failure and is suppressed in CI, on non-interactive CLI runs
(no TTY), or via environment flags.

```toml
[updates]
notify = true   # set false to silence the "new release available" notices
```

`updates.notify` defaults to `true`. Set it from the setup UI (Finish step →
Updates), with `agentos config` (a `config.patch` on `updates.notify`), or by
editing the config file directly. The state file
`~/.agentos/state/update_notice.json` tracks the last check times (namespaced per surface,
e.g. `cli`, `webui`) for throttling; delete it to force a re-check. To silence the notices
for a single run/session without changing config, set `AGENTOS_NO_UPDATE_NOTICE=1`.

## Observability

AgentOS provides Prometheus metrics exposition, OpenTelemetry (OTLP) trace export, and automatic log retention pruning under the `[observability]` table:

```toml
[observability]
metrics_enabled = true
metrics_path = "/metrics"
otlp_enabled = false
otlp_endpoint = "http://localhost:4318"
otlp_headers = {}
otlp_service_name = "agentos"
log_retention_days = 14
log_retention_max_total_mb = 500
log_retention_sweep_interval_s = 3600.0
```

- **Prometheus Metrics**: `metrics_enabled` (default `true`) exposes Prometheus metrics at `metrics_path` (default `"/metrics"`). Core metrics (`agentos_queue_depth`, `in_flight_turns_total`, `turn_cancellations_total`, `queue_full_errors_total`) are exported with bounded cardinality.
- **OTLP Trace Export**: `otlp_enabled` (default `false`) sends trace events as OTLP spans over HTTP to `otlp_endpoint` (e.g. OpenTelemetry Collector).
- **Log Retention**: `log_retention_days` (TTL) and `log_retention_max_total_mb` (total size cap) bound disk growth in `~/.agentos/logs`. Background sweeps run every `log_retention_sweep_interval_s`.

## Downloads, Web Push and Direct Tool Calls

```toml
[artifacts]
legacy_session_key_urls = true    # keep the deprecated ?sessionKey= download URLs this release

[push]
web_enabled = true                # browsers may subscribe to approval notifications
vapid_subject = "https://useagentos.dev"   # VAPID `sub`: a mailto: or https: contact

[tools.invoke]
allow = []                        # tools lifted off POST /api/v1/tools/invoke's hard deny list
```

| Key | Default | Meaning |
| --- | --- | --- |
| `artifacts.legacy_session_key_urls` | `true` | `false` makes `/api/v1/artifacts/{id}?sessionKey=` and `/api/v1/attachments/{sha256}?sessionKey=` answer `404`. Clients use ticketed URLs from `artifacts.download`; see [HTTP API](http-api.md#ticketed-downloads). |
| `push.web_enabled` | `true` | Web Push for approvals. The VAPID key pair is generated once into `<state_dir>/web_push_vapid.json` (0600); subscriptions live in `<state_dir>/web_push.sqlite`. |
| `push.vapid_subject` | `https://useagentos.dev` | Contact push services may use to reach the sender. |
| `tools.invoke.allow` | `[]` | Exact tool names `POST /api/v1/tools/invoke` may run although it would refuse them (not declared read-only, or on its name floor). The configured `[tools]` policy still applies. File-only: config RPC writes cannot add an entry. |

All three are read per request: no restart.

## Inbound Webhooks

```toml
[hooks]
token = "<long random secret, not the gateway token>"
rate_limit_per_minute = 60        # per hook name; 429 beyond it
max_sessions_per_hook = 1         # distinct sessions one hook may deliver into per hour

[hooks.routes.github]             # POST /api/v1/hooks/github
agent = "main"
session_key = ""                  # empty: agent:main:hook:github
template = "PR {{ payload.pull_request.title }} was {{ payload.action }}"

[hooks.lifecycle]                 # shell commands, run with a time budget
startup = []
shutdown = ["/usr/local/bin/backup-agentos"]
pre_restart = []
```

- `hooks.token` admits `POST /api/v1/hooks/agent`, `/wake` and `/<name>`
  (`Authorization: Bearer` or `X-AgentOS-Hook-Token`). The gateway token is
  refused there, and a hooks token equal to it disables the routes. Unset: the
  routes answer `404`. Wrong tokens are throttled by `[auth.rate_limit]` in a
  budget of their own: a sender that keeps guessing is locked out of the hooks
  (`429`), not out of the gateway.
- A route's `template` fills `{{ payload.a.b }}` placeholders (dotted paths,
  list indexes like `commits.0.id`) from the posted JSON; no expressions. Empty:
  the body's `message`, else the JSON body itself. A route's `session_key` is
  the only way to deliver into an existing conversation; a caller may only
  choose a key under `agent:<agent>:hook:`.
- `[hooks.lifecycle]` commands run through `/bin/sh -c` without the gateway's
  credentials in their environment, with `AGENTOS_LIFECYCLE_EVENT` and
  `AGENTOS_LIFECYCLE_REASON` set. Budgets: `startup` 10 s, `shutdown` 5 s,
  `pre_restart` 10 s, shared by every command and in-process hook of that event.
  They need a POSIX shell: on Windows each one is logged as failed
  (`gateway.lifecycle_hook_failed`) and not started.
- `hooks.token` and `hooks.lifecycle` are file-only: `config.set`,
  `config.patch` and `config.apply` refuse to set a token or add a command
  (clearing them is allowed).

Route names are 1-64 of `a-z 0-9 _ -` and cannot be `agent` or `wake`. Hooks
settings apply on the next request. See [HTTP API → Inbound
webhooks](http-api.md#inbound-webhooks).

## Raw Config Editing

For advanced settings, inspect `agentos.toml.example` and edit the active
config file directly. Use CLI commands for routine provider, router, search,
channel, and sandbox changes because they avoid common key-shape mistakes.

### Editing the file while the gateway runs

With `[gateway] config_reload = true` (the default) the gateway checks the
active config file every 2 seconds and, once a change has been still for half
a second, applies it like a config write from the console: validated, applied
in place, hot settings live at once. Settings that are only read at startup
(bind, channels, sandbox, memory backend, middleware, and anything not on the
audited live list) are recorded as pending instead: `agentos doctor` reports
`gateway.config.restart_pending` and the console gets a `config.changed`
event listing `hot` and `restartRequired` paths. Restart to apply those.

An edit that does not parse or validate never replaces the running config: the
gateway keeps its last good settings, logs `config.reload_skipped`, sends
`config.changed` with `status: "invalid"`, and `agentos doctor` reports
`gateway.config.reload_skipped` until a good save (fixing the file back to
the running settings sends `config.changed` with `status: "recovered"`). `--listen` / `--port`
overrides and runtime-only secrets (an auto-generated token, keys from the
environment) carry over a reload. The gateway's own writes are recognized and
cause no reload.

When `config_reload` is off, restart after changing the file. Either way, check:

```sh
agentos doctor
agentos gateway status
```

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
