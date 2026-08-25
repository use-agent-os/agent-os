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
  `AGENTOS_AGENT_PERMISSIONS`, `AGENTOS_HOOKS`, `AGENTOS_GATEWAY_TOKEN`,
  `AGENTOS_GATEWAY_CONFIG_PATH`, `AGENTOS_STATE_DIR`, `AGENTOS_ROOT`, and the
  bind settings

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
agentos config set skills.config.wiki.path /srv/wiki
```

When the agent opens the skill, the values currently in effect are appended to
what it reads, so it does not have to ask or go looking.

## Skill Prompt Budget

Every eligible skill is listed for the agent in a block appended to the system
prompt. `[skills].max_skills_prompt_chars` caps how large that block may get:

```toml
[skills]
max_skills_prompt_chars = 24000
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
agentos configure image-generation
agentos configure memory-embedding
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

```toml
[agentos_router]
strategy = "pilot-v1"   # default; or "llm_judge"
```

Router runtime dependencies (`onnxruntime`, `numpy`, `tokenizers`) stay in the
`recommended` / `ml-router` extras rather than the core install: a minimal
install without them does not fail — the router degrades to the default tier
and emits `pilot_unavailable` telemetry.

The supported strategies are also selectable from the Mode dropdown in
onboarding (Web UI wizard and CLI), a three-option selector: **Local ML —
English-optimized (Pilot)** (`pilot-v1`, the default), **Smart routing
(LLM-based)** (`llm_judge`), or **Off**. The legacy **Smart routing
(on-device)** (`v4_phase3`) option is no longer offered. The "Judge model" field
only appears for the LLM-based strategy; the "Pilot safety net" field only
appears for the Pilot strategy.

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
| `browser.binary_path` | `""` | Optional explicit path to `agent-browser`; otherwise found on `PATH`. |
| `browser.cdp_port` | `0` | `0` = managed. `>0` = attach to your Chrome's debug port. Localhost only; a URL is never accepted. |
| `browser.attach_confirmed` | `false` | Must be `true` for attach mode to run — it can drive signed-in sessions. |
| `browser.allowed_domains` | `[]` | `[]` = open web (SSRF still blocks private ranges). A non-empty list bounds navigation in AgentOS and in the engine. |
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

Read: [`features/memory.md`](features/memory.md)

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
currently in use are `document` and `vision`; a task with a capability
requirement wants its own entry, since a text-only model cannot describe an
image.

Resolution runs highest-first: `AGENTOS_<TASK>_MODEL` (for example
`AGENTOS_VISION_MODEL`), then `[auxiliary.tasks.<task>]`, then a
capability-aware default such as the router's image-capable tier, then
`[auxiliary]`, then `AGENTOS_LLM_MODEL`, then `[llm]`. An install that sets
none of this keeps using the `[llm]` model exactly as before.

These calls are billed to the session that triggered them and are additionally
tracked under an `aux:<task>` scope, so `agentos cost` can separate what the
agent spent answering from what the runtime spent on its own.

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
3. `AGENTOS_LISTEN`
4. `AGENTOS_GATEWAY_HOST`
5. config host
6. `127.0.0.1`

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

Related: `agentos upgrade` (the primary upgrade path), the version-skew policy,
and the `AGENTOS_ALLOW_VERSION_SKEW=1` escape hatch are documented in the
[README Upgrade section](../README.md#upgrade).

## Raw Config Editing

For advanced settings, inspect `agentos.toml.example` and edit the active
config file directly. Use CLI commands for routine provider, router, search,
channel, and sandbox changes because they avoid common key-shape mistakes.

After changing files by hand, restart the gateway and run:

```sh
agentos doctor
agentos gateway status
```

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
