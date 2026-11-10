# Skills

Skills are task-specific instruction packages and scripts. They let AgentOS
load relevant guidance only when a task needs it, instead of putting every
possible instruction into every prompt.

Skills are separate from memory. Memory stores facts; skills describe repeatable
ways to work.

## What Skills Are For

Use skills for repeatable work patterns such as:

- deep research;
- summarization;
- GitHub and PR workflows;
- document generation;
- spreadsheet, slide, PDF, and DOCX work;
- web search;
- weather lookup;
- terminal or tmux monitoring;
- subagent delegation;
- skill creation and review.

## Discover Installed Skills

List skills available in the current install:

```sh
agentos skills list
```

View one skill:

```sh
agentos skills view <skill-name>
```

Search community sources:

```sh
agentos skills search pdf
```

Some skills may be ineligible when optional dependencies are missing or when the
skill is intentionally demo-only. `skills list` is the source of truth for your
current install.

### Where Search Results Come From

`skills search` fans out to every configured source and merges the results. The
Bankr source covers both places Bankr publishes: skills in the
[BankrBot/skills](https://github.com/BankrBot/skills) repository, and skills
published from bankr.bot, which live under their author's wallet address
(`https://bankr.bot/skills/<wallet>/<slug>`) and are served with their body
inline rather than as a repository directory. Both install the same way:

```sh
agentos skills install https://bankr.bot/skills/<wallet>/<slug> --source bankr
```

Each half carries a fixed allowlist, because neither has an index that can be
crawled without tripping a rate limit — and because the source a skill arrives
through is recorded as its provenance, so it must not be able to pull an
arbitrary repository or another author's skill under Bankr's name. A skill
published from a wallet is credited to its author, not to Bankr: it lists the
author's handle and resolves to no recognized publisher, so it renders unbranded
rather than sitting in the Partners group.

The Aeon source reads
[aeonfun/aeon](https://github.com/aeonfun/aeon), which unlike the others
publishes a real machine-readable index — one `catalog/skills.json` describing
all 75 of its skills, so browsing costs a single request rather than one per
skill. Its allowlist therefore exists for a different reason: not because the
catalog cannot be crawled, but because most of what Aeon publishes does not
belong in an interactive runtime. Aeon skills are prompts written for scheduled
GitHub Actions runs, and dozens of them either operate an Aeon instance
(`aeon-doctor`, `memory-flush`, `aeon-update`) or read Aeon's durable `memory/`
directory as their actual input.

The allowlisted ones are adapted on install. Their frontmatter declares
`requires` as a plain list, which AgentOS reads only as a mapping and would
otherwise discard — so it is rewritten into the `requires.env` form, and the
Aeon category is remapped onto a real browse bucket. A short note is inserted
ahead of the skill's own text saying that `memory/`, `./secretcurl`, and
`./notify` are absent here and what to do instead. The prose itself is left
alone, so the installed file still reads as Aeon wrote it.

Some Aeon skills want an API key you supply yourself, and a few want a paid
plan; each catalog card lists what it needs under its setup steps.

## How a Skill Is Described

Four separate facts describe an installed skill. They are easy to confuse, and
using one to answer another is the usual cause of a confusing Skills screen.

| Fact | Question it answers | Values |
| --- | --- | --- |
| Acquisition | How did it get here? | `shipped`, `hub`, `local` |
| Publisher | Whose name is on it? | An allowlisted partner, or nothing |
| Layer | Where are the files? | `extra`, `bundled`, `managed`, `personal`, `project`, `workspace` |
| Availability | Is the agent being offered it right now? | offered, or a reason |

**Acquisition** is what the Web UI groups by, and what `agentos skills list
--json` reports under `acquisition`. A `shipped` skill came with AgentOS. A
`hub` skill was fetched by `agentos skills install` and has a lockfile entry
recording its source, identifier, version, and install time. A `local` skill is
a directory you put in a skills folder yourself.

The same block carries `removable` and `updatable`. These are answers, not
guesses from the layer: if a hub-installed skill's recorded path no longer
matches the configured `skills.managed_dir`, `removable` is `false` — AgentOS
will not delete files it cannot prove it owns — while `updatable` stays `true`,
because an update re-fetches by identifier into the current managed directory.

**Publisher** is allowlisted inside AgentOS. A skill manifest can only *select*
a recognized publisher by id; it can never *describe* one. A third-party skill
that writes a partner's name, URL, and logo into its own frontmatter renders as
an ordinary unbranded skill, because none of those fields are read.

Selecting an id is also restricted by where the skill lives. Only a `bundled`
manifest — one that ships inside the release — may name its own publisher; a
directory you (or anything else) drop into a writable skills path gets no brand
from its own frontmatter, so a look-alike cannot sit in the Partners group. An
installed partner skill is branded by the hub catalog row that installed it,
recorded in the lockfile, not by its own text. This is why a partner's skills
sit under one heading whether they shipped with AgentOS or you installed them
from that partner's hub.

**Layer** is only about file location and name-collision precedence (later
overrides earlier): `extra` (config dirs) → `bundled` (shipped) → `managed`
(`~/.agentos/skills`, where installs land) → `personal` (`~/.agents/skills`) →
`project` (`<workspace>/.agents/skills`) → `workspace` (`<workspace>/skills`).
It is shown as a detail on each card and it decides which skills are sacrificed
first if the prompt budget is exceeded, but it does not tell you where a skill
came from.

The two `.agents/skills` layers are shared with other agents on the machine —
Codex, Cursor, and anything else following that convention install there too.
AgentOS treats them as ordinary layers: a skill another tool writes appears on
the next turn, and one it removes stops being offered, without a restart and
without going through `agentos skills`. The directories are also honoured when
they do not exist yet, so the first cross-agent install on a fresh machine does
not need one either. Note the precedence that follows from the order above: a
skill in `.agents/skills` overrides a bundled or hub-installed skill of the same
name.

Provenance (`origin`, `license`, `upstream_url`) is separate from all four: it
records where the *text* came from and under what licence. A skill can be
AgentOS-original text published by a partner, or upstream text with no
publisher at all.

**Category** (`metadata.agentos.category`) is subject matter, not trust — what a
skill is about, where `capabilities` says what it can reach. The Web UI reads one
value from it: a shipped skill declaring `category: crypto` is split out of
"AgentOS Normal Skills" into its own **AgentOS Crypto Skills** heading. The split
is gated on acquisition exactly the way publishers are, so a directory dropped
into a writable skills path cannot claim a heading carrying the AgentOS name just
by writing a category into its own frontmatter.

Cards in that group carry a mark instead of the generic glyph. The nine bundled
GMGN skills (`gmgn-token`, `gmgn-market`, `gmgn-portfolio`, `gmgn-track`,
`gmgn-holder-analysis`, `gmgn-wallet-analysis`, `gmgn-wallet-score`,
`gmgn-swap`, `gmgn-cooking`) wear the GMGN mark — chosen on
`provenance.origin: gmgn-mit`, badged with each skill's own
`metadata.agentos.emoji` so nine siblings stay tellable apart — and the rest of
the group wears the AgentOS mark. Every mark ships with the client; no manifest
field points the UI at an image, remote or local.

## Declaring the Variables a Skill Reads

`metadata.agentos.requires.env` (or `metadata.platform.requires.env`) lists
the environment variables a skill's scripts read. Each entry carries a
`description` and a `url` so the Environment page and `agentos env list` can
say what the variable is for and where to obtain it, and `secret` when the
name-based heuristic would guess wrong. `required` defaults to `true`; set it
to `false` for a variable that unlocks part of the skill rather than gating all
of it -- the `multi-search-engine` skill declares every engine key that way, so
it stays offered on DuckDuckGo alone and grows engines as keys appear. A
required variable that is missing hides the skill and is reported by
`agentos skills doctor`; an optional one only shows as unset on the
Environment page.

Declared variables are also the ones forwarded into the `execute_code`
sandbox when the skill is viewed, which is how a script running there can
read its own key. AgentOS's own provider credentials are refused to hub and
local skills; a bundled skill may declare them.

## How a Skill Reaches Its Own Scripts

A `SKILL.md` is written before anyone knows where it will be installed or which
Python will be running, so it names both through placeholders that the tool
layer fills in when the body is handed to the agent (`skill_view`):

| Placeholder | Becomes | Why |
| --- | --- | --- |
| `{baseDir}` | The skill's own install directory | Bundled skills live under site-packages on one machine and under a workspace on the next; an absolute path is wrong everywhere but where it was typed. |
| `{python}` | The interpreter AgentOS itself runs on (`sys.executable` of the gateway) | A bare `python` in a shell command is whatever the user's PATH says — a Homebrew 3.9 with a stray `httpx`, or nothing at all on Windows. Only AgentOS's own interpreter is guaranteed to carry the skill's dependencies and to be new enough for its syntax. |

So a script invocation in a skill body is written as:

```bash
{python} {baseDir}/scripts/run.py --flag value
```

and an `entrypoint.command` in frontmatter as `"{python} {baseDir}/scripts/run.py"`
(quoted — YAML would otherwise read the leading `{` as a mapping). `agentos
skills init --with-script` scaffolds it that way. The expanded text also leads
with `[Skill directory: …]` and `[Skill interpreter: …]` lines, so a third-party
skill that still says `python3 script.py` can be steered onto the right
interpreter too.

Expansion happens only on the copy the agent reads. The file on disk keeps the
placeholders, which is what lets `skill_edit` write it back without baking a
machine-specific path into it, and what lets the same skill directory move
between machines.

## Whether the Agent Is Offered a Skill

Installed, eligible, and offered are three different states. A skill can be
installed and fully eligible and still not reach the agent on a given turn.

The gateway reports this as `availability` on every skill row, and the agent's
own skill listing prints a `[not offered]` line with the same explanation:

| Reason | Meaning | What to do |
| --- | --- | --- |
| offered | the agent has it | — |
| `model_invocation_disabled` | the manifest opts out of model invocation | nothing; run it yourself |
| `ineligible` | a required binary, environment variable, or OS is missing | the detail names what is missing |
| `tool_gate` | its required tools are not enabled in this session | enable the tools |
| `fallback_superseded` | it is a fallback for a tool the session already has natively | nothing; the native tool is better |
| `not_retrieved` | relevance filtering is on and this message did not match | raise `skills.filter_top_k`, or reword |
| `prompt_budget` | ready, but the skills block is full | raise `skills.max_skills_prompt_chars` |

Every reason above appears in a `skills.list` row except `not_retrieved`.
Retrieval ranks skills against the wording of one message, so there is no query
to answer it with outside a turn — it reaches the decision log, never the Skills
page. The other six, `prompt_budget` included, are answerable from the installed
set plus the configured budget, so the page can show them before you send
anything.

`agentos skills list --json` does not carry `availability`. A CLI process has
no chat session and no tool surface, so it cannot answer honestly; an absent
key means "not computed", never "not offered".

## Install, Update, and Remove Skills

Install a managed skill:

```sh
agentos skills install <skill-name>
```

Update one skill or all managed skills:

```sh
agentos skills update <skill-name>
agentos skills update --all
```

Remove a managed skill:

```sh
agentos skills uninstall <skill-name>
```

### Opt-in skills in this repository

`src/agentos/skills/exp/` is left out of the wheel, so nothing in it is in a
fresh install's skill list or prompt. A skill there that a user wants is
installed from GitHub like any other repository directory, through the same
scan and install policy:

| Skill | What it does | Install |
|---|---|---|
| `musedin` | Profile, feed, roles and jobs on MusedIn (musedin.com), signed with the identity the bundled `musebook` skill stores. Paid x402 actions only when the user asks; the script itself never pays. | `agentos skills install https://github.com/use-agent-os/agent-os/tree/main/src/agentos/skills/exp/musedin --source github` |

## Install Security

A hub skill is someone else's instructions and scripts, so an install passes
three checks before anything reaches the managed layer. The bundle is staged in
`~/.agentos/quarantine` while they run and deleted if any of them refuses.

| Check | Refuses when | `--force` |
| --- | --- | --- |
| Registry verdict | ClawHub's own review marks the skill `suspicious` | overrides |
| Registry verdict | ClawHub marks it `malicious` | never overrides |
| Built-in scan | the pattern scan finds prompt injection, exfiltration or hidden Unicode (`dangerous`) | overrides |
| Install policy | your policy command answers `warn` | overrides |
| Install policy | your policy command answers `block`, or fails to answer | never overrides |

**Registry verdict.** ClawHub scans what it hosts with ClawScan and publishes
the result. AgentOS reads it on install, refuses on `suspicious` or `malicious`,
and records it in the lockfile as `registry_verdict`. Search results carry it
too, along with `upstream_scanners` — third-party scanner statuses the registry
relays (Snyk, Socket and others). Those are shown but never refused on: they
are another party's opinion of another party's copy. Other sources publish no
verdict, so the field is empty for them.

**Install policy.** For a real review, point AgentOS at a scanner of your own.
It is off by default:

```toml
[skills.install_policy]
enabled = true
command = "/absolute/path/to/clawscan"
args = ["openclaw-install-policy"]
pass_env = ["PATH", "DOCKER_HOST"]
timeout_seconds = 300
```

The command is run once per install and per update, after the built-in scan
and before the move. It reads one JSON request on stdin — the quarantined
directory as `sourcePath`, the skill name, the source and the requested
identifier — and writes one JSON response on stdout:

```json
{"protocolVersion": 1, "decision": "block", "reason": "reads ~/.ssh/id_rsa"}
```

`decision` is `allow`, `warn` or `block`; `reason` and a `findings` list are
optional. This is the OpenClaw `security.installPolicy` protocol, version 1, so
`clawscan openclaw-install-policy` works unchanged and can front SkillSpector,
Tencent AIG, Cisco's scanner or Snyk. Any executable that speaks the protocol
will do.

The policy fails closed. A command that is missing, is not an absolute path,
exits nonzero, times out, or prints anything other than a valid response blocks
the install, and so does `enabled = true` with no `command`. The command
inherits only the variables named in `pass_env`, not the gateway's environment,
and it runs outside the tool sandbox as a trusted local process — point it at a
binary you control.

The policy covers `agentos skills install` and `agentos skills update`,
Settings › Skills in the web console, and the agent's `skill_install_community` tool. It does not cover
skills you write yourself or copy into a skill directory by hand.

## Manage Skill Sources

Custom source repositories are called taps:

```sh
agentos skills tap list
agentos skills tap add <owner/repo>
agentos skills tap remove <owner/repo>
```

Use taps when your team maintains its own skill catalog.

## What a Skill Can Put in the Chat

Besides text, a skill hands files to the surface with `publish_artifact`. Most
arrive in Web UI chat as a download chip, but some mimes render inline instead:

| Mime | Rendered as |
| --- | --- |
| `image/*` | Inline preview |
| `audio/*` | Inline player |
| `application/vnd.agentos.chart+json` | Interactive candlestick chart |

Nothing has to be registered for this: the mime a skill publishes decides how
its output is drawn, so any skill can render a chart without a frontend change.

[`artifacts-and-media.md`](../artifacts-and-media.md#inline-charts) carries the
payload shape, the workspace rule `publish_artifact` enforces, and the two
mistakes that produce no error and no chart — omitting the mime, and writing the
file outside the workspace.

## Publish and Inspect

Publish a skill directory:

```sh
agentos skills publish <path-to-skill>
```

For ordinary skill content, use:

```sh
agentos skills view <skill-name>
```

## How to Ask for a Skill

Ask for the outcome:

```text
Create a PowerPoint deck summarizing this report.
```

Better than:

```text
Load the pptx skill and run its script.
```

AgentOS can choose eligible skills from the current catalog when the task
matches their description and triggers.

## Bundled Skill Families

| Family | Examples |
| --- | --- |
| Research | deep research, multi-source search, summarization |
| Documents | DOCX, PPTX, XLSX, PDF, HTML-to-PDF |
| Operations | cron, GitHub, terminal monitoring, subagents |
| Memory | memory-oriented helpers and history exploration |
| Creation | skill review |

## Troubleshooting

If a skill is not used, start by finding out whether the agent was ever offered
it. Guessing from the layer or from "it says ready" is what makes this hard.

1. Confirm it appears in the installed catalog:

   ```sh
   agentos skills list
   ```

2. Open the skill on the Skills screen, or ask the agent to list its skills.
   Either surface names the reason it is being withheld. Match it below.

3. **"Needs setup" / `ineligible`.** The reason names the missing binary or
   environment variable. Install the binary, or set the variable:

   ```sh
   agentos env set <NAME> --stdin
   ```

   The value applies to the running gateway — no restart — and the skill
   becomes eligible on the next turn.

4. **"Skills section is full" / `prompt_budget`.** The skill is ready and was
   dropped only because the injected skills block hit its character budget.
   Raise it:

   ```sh
   agentos config get skills.max_skills_prompt_chars     # default 24000
   agentos config set skills.max_skills_prompt_chars 32000
   agentos gateway restart
   ```

   The gateway also logs `skills_filter.budget_truncated` with the names it
   dropped and the budget it hit. Truncation goes lowest-precedence layer
   first — `extra` directories, then `bundled` — so the symptom is usually
   shipped skills quietly disappearing while what you installed into
   `managed`, `personal`, `project`, or `workspace` survives.

5. **The agent ignores skills, but nothing reports `prompt_budget`.** The block
   fits, so no skill was dropped — but the budget was tight enough that the
   descriptions were shortened, or replaced by names alone. A name is rarely
   enough for the model to tell that a skill applies, so the symptom is an agent
   that answers from general knowledge with every skill still listed as offered.
   Check the render mode:

   ```sh
   grep skills_render_mode ~/.agentos/logs/decisions-*.jsonl | tail -1
   ```

   Anything other than `full` means the budget cost the model description text,
   and the gateway logged `skills_filter.budget_degraded` when it happened.
   Raise `skills.max_skills_prompt_chars`, or turn on `skills.filter_enabled` so
   only the skills relevant to each message are injected.

6. **`not_retrieved`.** Only reachable when `skills.filter_enabled = true`,
   which is off by default. Raise `skills.filter_top_k` or reword the request;
   the answer depends on the wording of that one message.

7. **`tool_gate` or `fallback_superseded`.** These are about the session's
   tools, not the skill. The first means a tool the skill needs is not enabled;
   the second means AgentOS already has a native tool that does the job better.

8. **`model_invocation_disabled`.** Working as declared. The skill opted out of
   model invocation and is for you to run, not the agent.

9. If none of the above applies, ask for the outcome in normal language. Skill
   names can help, but user intent should still be clear.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
