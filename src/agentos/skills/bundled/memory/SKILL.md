---
name: memory
description: "Use when the user asks to remember, recall, forget, update, search, or inspect durable AgentOS memory, including profile facts in USER.md and long-term notes in MEMORY.md or memory/**/*.md."
always: false
triggers:
  - remember
  - recall
  - forget
provenance:
  origin: agentos-original
  license: MIT
  upstream_url: ""
  maintained_by: AgentOS
metadata:
  agentos:
    requires_tools:
      - memory_search
      - memory_get
---

# AgentOS Memory

Use only tools that are visible in the current tool list. This skill explains
AgentOS's memory source files; it does not make hidden tools available.

## Source Files

- `USER.md`: stable user profile fields such as name, preferred address,
  pronouns, timezone, and durable profile notes. Edit it with visible
  filesystem tools, not `memory_save`.
- `MEMORY.md`: curated long-term non-profile facts, preferences, decisions,
  and constraints.
- `memory/YYYY-MM-DD.md` and `memory/**/*.md`: daily, session, or named memory
  notes.
- `turns/**/*.md`: private auto-captured turn state. These files are for
  audit/future processing and are not indexed or returned by ordinary
  `memory_search`.

The Markdown files are the source of truth. The memory index/database is derived
from curated `MEMORY.md` and `memory/**/*.md` files only.

## Recall

- Use injected `USER.md` first for current user identity/profile questions.
- Use `memory_search` for historical or non-profile recall that is not already
  in injected context.
- Use `memory_get` after search when exact lines or more context are needed.

## Remember Or Update

- If the user specifies a memory path, use that exact path if it is a valid
  memory source file.
- For profile facts, edit `USER.md` with visible filesystem tools.
- For daily or session notes, write to `memory/YYYY-MM-DD.md` or another
  appropriate `memory/**/*.md` source.
- For curated long-term facts, use the `memory` tool when it is visible:
  `memory(action=add|replace|remove, target=memory|user, content=..., old_text=...)`.
  Use `target=memory` for durable non-profile facts and `target=user` for
  profile facts. Make all related changes in one call via `operations` when
  batching adds/replaces/removes together.
- `memory_save` never accepts `MEMORY.md` -- it always rejects that path. Use
  it only for `memory/**/*.md` notes (daily or named), never for `MEMORY.md`
  or `USER.md`.
- If `memory_save` is not available but filesystem tools are visible, edit or
  create the same source files directly.

## Forget Or Correct

- Search first, then read the relevant file/lines before removing anything.
- If `memory_delete` is available, use it only when the user wants to delete a
  whole memory source file.
- To remove or correct one fact, edit the source file directly when filesystem
  tools are visible.
- If no write or delete tool is available, report the exact path and lines that
  should be changed instead of claiming the memory was updated.

## Boundaries

- Do not store ordinary deliverables such as reports, JSON outputs, or result
  files in memory source files.
- Do not save secrets, tokens, private keys, or full credential contents.
- Only confirm memory was updated after the write or delete succeeds.
