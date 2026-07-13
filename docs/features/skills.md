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

## Manage Skill Sources

Custom source repositories are called taps:

```sh
agentos skills tap list
agentos skills tap add <owner/repo>
agentos skills tap remove <owner/repo>
```

Use taps when your team maintains its own skill catalog.

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

If a skill is not selected:

1. Confirm it appears in the installed catalog:

   ```sh
   agentos skills list
   ```

2. Inspect its description and eligibility:

   ```sh
   agentos skills view <skill-name>
   ```

3. Ask for the outcome in normal language. Skill names can help, but user
   intent should still be clear.

4. If optional dependencies are missing, install or update the skill and retry.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
