# CLAUDE.md

This project keeps a single source of truth for agent instructions in
**[AGENTS.md](AGENTS.md)**. Read it — setup, the quality gate (uv + ruff +
mypy + pytest), source layout, conventions, and commit/PR rules all live there.

One rule worth repeating here: whenever you update the CLI surface (commands,
flags, config keys, ports, paths), always check
`src/agentos/skills/bundled/agentos/SKILL.md` and `docs/cli.md` and bring them
up to date in the same change.
