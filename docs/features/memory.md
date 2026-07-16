# Memory

AgentOS memory helps the agent recall durable context without replaying
every old conversation. Use it for stable preferences, reusable project facts,
previous decisions, and notes that should survive across sessions.

Memory is separate from skills. Skills teach the agent how to do a task; memory
stores useful facts and context the agent may need later.

## What to Store

Good memory entries are stable and reusable:

- user preferences;
- project conventions;
- recurring output formats;
- names of important repositories, directories, or services;
- decisions the user wants reused;
- brief notes from completed tasks.

Avoid memory for:

- API keys or secrets;
- raw private data that does not need long-term recall;
- one-off instructions for the current turn;
- noisy dumps that would pollute future retrieval;
- exact transcripts that should instead be exported as session records.

## Common Commands

Inspect memory health:

```sh
agentos memory status
agentos memory status --deep
```

Index and list memory sources:

```sh
agentos memory index
agentos memory list
```

Search and inspect memory:

```sh
agentos memory search "release note format"
agentos memory show <path>
```

Search previous sessions as well as memory:

```sh
agentos memory search "deployment decision" --source all
```

## Natural Chat Usage

Ask naturally when something should be remembered:

```text
Remember that I prefer concise release notes with a risk section.
```

Later, refer to the preference:

```text
Use my usual release-note format for this changelog.
```

When memory seems stale, ask the agent to search explicitly:

```text
Search memory for my release-note preferences before drafting this.
```

## Session-Derived Memory

For long or important sessions, flush session state into memory before
archiving, compacting, or switching tasks:

```sh
agentos memory flush-session <session-key>
```

Use session export when exact old wording matters:

```sh
agentos sessions export <session-key>
```

Memory is for useful recall. Session export is for exact records.

## Curated memory (MEMORY.md / USER.md)

Two small, bounded files are injected into every system prompt so the agent
always has the highest-signal facts on hand without a retrieval round-trip:

- `MEMORY.md` — the agent's own notes (environment facts, conventions, tool
  quirks, lessons learned).
- `USER.md` — what the agent knows about the user (name, role, preferences,
  style).

Both stores live at the workspace root and are §-delimited lists of small
entries, not free-form prose. Each store is char-budgeted rather than
line- or entry-limited:

- `memory.curated_memory_char_limit` (default `4000`) bounds `MEMORY.md`.
- `memory.curated_user_char_limit` (default `2000`) bounds `USER.md`.
- `memory.inject_limit` (default `6400`) bounds the combined text actually
  injected into the system prompt. It is kept comfortably above the sum of
  the two budgets plus header overhead so a full `MEMORY.md` and a full
  `USER.md` both fit without either block being dropped. If a block would
  push the joined result over `inject_limit`, that block (and any
  lower-priority block after it) is dropped whole rather than sliced
  mid-block — memory is checked first, then user.

### The `memory` tool

Writes go through a single `memory` tool, not free-form file edits:

```json
{"action": "add", "target": "memory", "content": "Deploys with make deploy"}
{"action": "replace", "target": "user", "old_text": "prefers dark mode", "content": "Prefers dark mode and 2-space indent"}
{"action": "remove", "target": "memory", "old_text": "stale fact"}
```

`target` is `"memory"` (default) or `"user"`. Multiple changes in one turn
should use the batch shape instead, which applies atomically against the
*final* budget:

```json
{
  "target": "memory",
  "operations": [
    {"action": "remove", "old_text": "old deploy note"},
    {"action": "add", "content": "Deploys with make deploy"}
  ]
}
```

### Consolidation when full

An `add` (or a batch whose final result) that would exceed the store's char
limit is rejected with the current entries returned in the response, so the
agent can consolidate: `replace` overlapping entries or `remove` stale ones,
then retry — ideally in the same batch call so a single round-trip both
frees room and adds the new fact. Repeated consolidation failures within a
turn cap out after a few attempts so a fragile add/replace can't loop the
turn to budget exhaustion.

### Drift guard (`.bak` files)

Each store expects its file to be a clean §-delimited list it wrote itself.
If an external writer (a patch tool, shell append, manual edit, or a
concurrent session) leaves content on disk that would not round-trip through
that format, the next `replace`/`remove`/batch call is refused rather than
silently discarding the foreign content. A timestamped snapshot is written
next to the file (e.g. `MEMORY.md.bak.<unix_ts>`) and the tool response
points at it so the drift can be reviewed and reconciled before retrying.

### Migration note

Agents that had a free-form `MEMORY.md` (headings, bullet lists, paragraphs)
from before curated memory existed are migrated automatically, once, the
first time the curated store loads: the text is split into entries and kept
up to 80% of the char budget, with any remainder archived to
`memory/archive/memory-overflow.md` (still indexed and searchable — nothing
is lost). `MEMORY.md` is rewritten in place as a clean §-delimited list.

### `memory_save` scope

`memory_save` now targets only `memory/**/*.md` notes — it no longer accepts
`MEMORY.md` directly. Durable facts about the agent or the user go through
the `memory` tool described above instead.

## Maintenance and Repair

Refresh the index after editing memory files or changing memory configuration:

```sh
agentos memory index --force
```

Inspect fallback and repair surfaces:

```sh
agentos memory raw-fallbacks list
agentos memory repair list
```

Show or repair a degraded compaction memory record when instructed by
diagnostics:

```sh
agentos memory repair show --summary-id <id>
agentos memory repair run --summary-id <id>
```

## Best Practices

- Keep entries short and sourceable.
- Prefer "Remember X for project Y" over vague "remember this."
- Search memory before assuming the agent forgot.
- Remove or revise stale preferences instead of adding contradictory ones.
- Keep secrets out of memory.
- Use artifacts or files for large reference material.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
