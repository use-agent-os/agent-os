"""CLI commands for skill management."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from agentos.cli.gateway_rpc import (
    default_gateway_token,
    default_gateway_url,
    rpc_error_exit_code,
    run_gateway_sync,
)
from agentos.cli.output import emit_error, print_json
from agentos.cli.ui import ACCENT, console

skills_app = typer.Typer(help="Skill management - list, search, install, uninstall.")


def _install_result_payload(result: Any) -> dict[str, Any]:
    payload = dict(result) if isinstance(result, dict) else asdict(result)
    scan = payload.get("scan")
    if scan is None:
        payload.pop("scan", None)
    return payload


async def _try_gateway_skill_mutation(
    method: str,
    params: dict[str, Any],
    *,
    json_output: bool,
) -> dict[str, Any] | None:
    """Use the running gateway when available; return None only for connect failures."""

    from agentos.cli import gateway_client as gateway_client_module

    client = gateway_client_module.GatewayClient()
    try:
        await client.connect(default_gateway_url(), token=default_gateway_token())
    except (SystemExit, ConnectionError, OSError):
        await client.close()
        return None

    try:
        payload = await client.call(method, params)
    except gateway_client_module.GatewayRPCError as exc:
        emit_error(
            exc.message,
            json_output=json_output,
            code=exc.code,
            details=exc.data,
        )
        raise typer.Exit(rpc_error_exit_code(exc.code)) from exc
    except (ConnectionError, OSError) as exc:
        emit_error(str(exc), json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    finally:
        await client.close()

    return payload if isinstance(payload, dict) else {"result": payload}


def _emit_skill_mutation_result(
    payload: dict[str, Any],
    *,
    json_output: bool,
    success_label: str,
    fallback_name: str,
) -> None:
    success = bool(payload.get("success", False))
    if json_output:
        print_json(payload)
        if not success:
            raise typer.Exit(1)
        return

    name = str(payload.get("name") or fallback_name)
    message = str(payload.get("message") or "")
    if success:
        path = payload.get("path")
        suffix = f" -> {path}" if path else ""
        console.print(f"[green]{success_label}:[/] {name}{suffix}")
        if message:
            console.print(message)
        return

    console.print(f"[red]Failed:[/] {message or name}")
    raise typer.Exit(1)


def _offline_loader() -> tuple[Any, Any]:
    """Build the layer loader (and its config) this process reads skills through.

    Only the no-gateway paths need it: every command prefers the running
    gateway, which already owns a loader. Extracted so ``skills list`` and
    ``skills uninstall`` resolve a skill the same way offline — they disagreed
    once and the CLI reported a skill it could not then remove.
    """
    import os
    from pathlib import Path

    from agentos.gateway.config import GatewayConfig
    from agentos.skills.loader import SkillLoader
    from agentos.skills.paths import resolve_skill_layer_dirs

    config = GatewayConfig.load(os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
    workspace_root = Path(config.workspace_dir) if config.workspace_dir else None
    workspace_override = Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
    layer_dirs = resolve_skill_layer_dirs(
        allow_bundled=config.skills.allow_bundled,
        workspace_root=workspace_root,
        workspace_override=workspace_override,
        managed_override=config.skills.managed_dir,
        extra_dirs=[Path(d) for d in config.skills.extra_dirs],
    )
    loader = SkillLoader(
        bundled_dir=layer_dirs.bundled_dir,
        workspace_dir=layer_dirs.workspace_dir,
        managed_dir=layer_dirs.managed_dir,
        personal_agents_dir=layer_dirs.personal_agents_dir,
        project_agents_dir=layer_dirs.project_agents_dir,
        extra_dirs=layer_dirs.extra_dirs,
    )
    return loader, config


def _offline_lock_key(name: str) -> str:
    """Return the lockfile key for ``name``, or ``name`` when nothing loads.

    The installer is keyed by the install directory while a user types the name
    a manifest declares; see
    :func:`~agentos.skills.inventory.lock_key_for_skill`. Best-effort: a broken
    config must not turn ``skills uninstall`` into a traceback, so a failure to
    build the loader falls back to the typed name — exactly what this command
    passed before.
    """
    from agentos.skills.hub.lockfile import Lockfile, default_lockfile_path
    from agentos.skills.inventory import lock_key_for_skill

    try:
        loader, _ = _offline_loader()
        spec = loader.get_by_name(name)
    except Exception:  # pragma: no cover - config/IO shapes vary by install
        return name
    if spec is None:
        return name
    return lock_key_for_skill(spec, Lockfile.load(default_lockfile_path()))


def _load_skill_rows() -> list[dict[str, Any]]:
    """Build the offline ``skills list`` rows from the shared inventory.

    This runs without a gateway, so it re-derives the layer directories, but the
    facts it reports — eligibility, acquisition, publisher — come from
    :func:`~agentos.skills.inventory.build_skill_inventory`, the same builder the
    RPC surfaces use. That is the point: the CLI and the Web UI used to answer
    "where did this skill come from" differently for the same skill.

    ``availability`` is the one block this surface omits. It is a question about
    a chat session's tool surface, and a CLI process has none; a fabricated
    verdict here would be worse than an absent key.
    """
    from agentos.skills.inventory import (
        acquisition_payload,
        build_skill_inventory,
        publisher_payload,
    )

    loader, config = _offline_loader()
    inventory = build_skill_inventory(loader, config=config)
    rows: list[dict[str, Any]] = []
    for row in sorted(inventory, key=lambda r: r.spec.name):
        skill = row.spec
        if not skill.user_invocable:
            continue
        provenance = getattr(skill, "provenance", None)
        rows.append(
            {
                "name": skill.name,
                "layer": skill.layer.value,
                "eligible": row.eligibility.eligible,
                "description": skill.description,
                "always": skill.always,
                "triggers": list(skill.triggers),
                "path": str(skill.path) if skill.path is not None else "",
                "filePath": skill.file_path,
                "baseDir": skill.base_dir,
                "homepage": skill.homepage,
                "userInvocable": skill.user_invocable,
                "disableModelInvocation": skill.disable_model_invocation,
                "provenance": {
                    "origin": provenance.origin if provenance else "unknown",
                    "license": provenance.license if provenance else "unknown",
                    "upstreamUrl": provenance.upstream_url if provenance else "",
                    "maintainedBy": provenance.maintained_by if provenance else "AgentOS",
                },
                # Emitted verbatim from the shared serializers, snake_case keys
                # and all, so a CLI row and an RPC row can be diffed field for
                # field. The camelCase keys above predate this and keep their
                # names — the payload is strictly additive.
                "publisher": publisher_payload(row.publisher),
                "acquisition": acquisition_payload(row.acquisition),
            }
        )
    return rows


@skills_app.command("list")
def skills_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List all installed/available skills."""
    rows = _load_skill_rows()
    if json_output:
        print_json(rows)
        return

    table = Table(title=f"Skills ({len(rows)})")
    table.add_column("Name", style=ACCENT)
    table.add_column("Layer")
    table.add_column("Eligible")
    table.add_column("Description")

    for row in rows:
        table.add_row(
            escape(row["name"]),
            row["layer"],
            "[green]yes[/]" if row["eligible"] else "[dim]no[/]",
            escape(
                row["description"][:60] + "..."
                if len(row["description"]) > 60
                else row["description"]
            ),
        )
    console.print(table)


@skills_app.command("search")
def skills_search(
    query: str = typer.Argument(..., help="Search query"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Search for skills across Community sources."""

    async def _search() -> None:
        from agentos.skills.hub.defaults import get_default_skill_router

        router = get_default_skill_router()
        results = await router.search(query, limit=20)

        if json_output:
            print_json([asdict(result) for result in results])
            return

        if not results:
            console.print(f"[dim]No results for '{query}'[/]")
            return

        table = Table(title=f"Search: {query}")
        table.add_column("Name", style=ACCENT)
        table.add_column("Source")
        table.add_column("Trust")
        table.add_column("Description")

        for r in results:
            # Remote catalogs are community-controlled — never let their text
            # be interpreted as rich markup.
            table.add_row(
                escape(r.name),
                escape(r.source_id),
                escape(r.trust_level),
                escape((r.description or "")[:60]),
            )
        console.print(table)

    asyncio.run(_search())


@skills_app.command("view")
def skills_view(
    name: str = typer.Argument(..., help="Skill name"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect a single skill from the running gateway."""

    async def _run(client):
        return await client.call("skills.get", {"name": name})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Skill: {payload.get('name', name)}")
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    for key in (
        "name",
        "layer",
        "eligible",
        "description",
        "file_path",
        "base_dir",
        "homepage",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            table.add_row(key, str(value))
    console.print(table)
    content = str(payload.get("content") or "")
    if content:
        preview = content if len(content) <= 1200 else content[:1200] + "\n..."
        console.print(Panel(preview, title="Content", expand=False))


@skills_app.command("update")
def skills_update(
    name: str | None = typer.Argument(None, help="Skill name to update"),
    all_skills: bool = typer.Option(False, "--all", help="Update all managed skills"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Update one managed skill, or all managed skills."""
    if bool(name) == all_skills:
        raise typer.BadParameter("provide exactly one of NAME or --all")

    async def _run(client):
        params = {} if all_skills else {"name": name}
        return await client.call("skills.update", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    results = payload.get("results", []) if isinstance(payload, dict) else []
    failures = [r for r in results if isinstance(r, dict) and not r.get("success", False)]
    top_level_failure = isinstance(payload, dict) and payload.get("success") is False
    if json_output:
        print_json(payload)
    else:
        table = Table(title="Skill updates")
        table.add_column("Name", style=ACCENT)
        table.add_column("Status")
        table.add_column("Message")
        for row in results:
            if not isinstance(row, dict):
                continue
            ok = bool(row.get("success", False))
            table.add_row(
                str(row.get("name") or ""),
                "[green]ok[/]" if ok else "[red]failed[/]",
                str(row.get("message") or ""),
            )
        console.print(table)
        message = payload.get("message") if isinstance(payload, dict) else None
        if message:
            console.print(str(message))
    if failures or top_level_failure:
        raise typer.Exit(1)


@skills_app.command("install")
def skills_install(
    identifier: str = typer.Argument(..., help="Skill name or identifier"),
    source: str = typer.Option(
        "clawhub",
        "--source",
        "-s",
        help=(
            "Source (clawhub, github, bankr, capminal, aeon). GitHub accepts owner/repo, "
            "owner/repo:path, "
            "or GitHub URLs. Bankr accepts a BankrBot/skills URL or a bankr.bot skill URL."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force install (skip the security block and the bundled-shadow refusal)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Install a skill from a Community source."""

    async def _install() -> None:
        payload = await _try_gateway_skill_mutation(
            "skills.install",
            {"identifier": identifier, "source": source, "force": force},
            json_output=json_output,
        )
        if payload is not None:
            _emit_skill_mutation_result(
                payload,
                json_output=json_output,
                success_label="Installed",
                fallback_name=identifier,
            )
            return

        from agentos.skills.hub.defaults import build_default_skill_installer

        installer = build_default_skill_installer()

        if not json_output:
            console.print(f"Installing '{identifier}' from {source}...")
        result = await installer.install(identifier, source, force=force)

        if json_output:
            print_json(_install_result_payload(result))
            if not result.success:
                raise typer.Exit(1)
            return

        if result.success:
            console.print(f"[green]Installed:[/] {result.name} → {result.path}")
            if result.scan and result.scan.verdict != "safe":
                scan = result.scan
                console.print(
                    f"[yellow]Security: {scan.verdict} ({len(scan.findings)} findings)[/]"
                )
        else:
            console.print(f"[red]Failed:[/] {result.message}")
            raise typer.Exit(1)

    asyncio.run(_install())


@skills_app.command("uninstall")
def skills_uninstall(
    name: str = typer.Argument(..., help="Skill name to remove"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Uninstall a managed skill."""

    async def _uninstall() -> None:
        payload = await _try_gateway_skill_mutation(
            "skills.uninstall",
            {"name": name},
            json_output=json_output,
        )
        if payload is not None:
            _emit_skill_mutation_result(
                payload,
                json_output=json_output,
                success_label="Uninstalled",
                fallback_name=name,
            )
            return

        from agentos.skills.hub.defaults import build_default_skill_installer

        installer = build_default_skill_installer()
        result = await installer.uninstall(_offline_lock_key(name))

        if json_output:
            print_json(_install_result_payload(result))
            if not result.success:
                raise typer.Exit(1)
            return

        if result.success:
            console.print(f"[green]Uninstalled:[/] {result.name}")
        else:
            console.print(f"[red]Failed:[/] {result.message}")
            raise typer.Exit(1)

    asyncio.run(_uninstall())


# ── Tap sub-commands ──────────────────────────────────────────────────────

tap_app = typer.Typer(help="Manage custom skill source repositories (taps).")
skills_app.add_typer(tap_app, name="tap")


@tap_app.command("add")
def tap_add(owner_repo: str = typer.Argument(..., help="GitHub owner/repo")) -> None:
    """Add a custom skill source tap."""
    from agentos.skills.hub.taps import TapsManager

    try:
        mgr = TapsManager()
        tap = mgr.add(owner_repo)
        console.print(f"[green]Added tap:[/] {tap.full_name} ({tap.url})")
    except ValueError as e:
        console.print(f"[red]Error:[/] {e}")


@tap_app.command("list")
def tap_list() -> None:
    """List registered taps."""
    from agentos.skills.hub.taps import TapsManager

    mgr = TapsManager()
    taps = mgr.list()
    if not taps:
        console.print("[dim]No taps registered.[/]")
        return
    for t in taps:
        console.print(f"  {t.full_name}  {t.url}  (added {t.added_at})")


@tap_app.command("remove")
def tap_remove(owner_repo: str = typer.Argument(..., help="GitHub owner/repo")) -> None:
    """Remove a tap."""
    from agentos.skills.hub.taps import TapsManager

    mgr = TapsManager()
    if mgr.remove(owner_repo):
        console.print(f"[green]Removed:[/] {owner_repo}")
    else:
        console.print(f"[yellow]Not found:[/] {owner_repo}")


# ── Publish command ───────────────────────────────────────────────────────


@skills_app.command("publish")
def skills_publish(
    skill_dir: str = typer.Argument(..., help="Path to skill directory"),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Target repo (owner/repo) for PR"),
) -> None:
    """Validate and publish a skill to a repository."""
    from pathlib import Path

    async def _publish() -> None:
        from agentos.skills.hub.publisher import publish_skill

        result = await publish_skill(Path(skill_dir), target_repo=repo)
        if result.success:
            console.print(f"[green]OK:[/] {result.message}")
        else:
            console.print(f"[red]Failed:[/] {result.message}")
            raise typer.Exit(1)

    asyncio.run(_publish())


# ── Init command ──────────────────────────────────────────────────────────


@skills_app.command("init")
def skills_init(
    name: str = typer.Argument(
        ...,
        help="The directory and skill name (must be a valid safe name)",
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="Short description of the skill",
    ),
    triggers: list[str] = typer.Option(
        None,
        "--trigger",
        "-t",
        help="Repeatable trigger terms that activate this skill",
    ),
    target_dir: Path = typer.Option(
        None,
        "--target-dir",
        "-p",
        help="Explicit parent target directory to place the skill in",
    ),
    with_script: bool = typer.Option(
        False,
        "--with-script",
        help="Additionally generate a scripts/run.py script template",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite generated files if they already exist, without deleting the directory",
    ),
) -> None:
    """Initialize a custom skill template with a compliant SKILL.md."""
    import re

    # 1. Validate name
    # SAFE_NAME_RE: ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$
    safe_name_re = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    if not safe_name_re.match(name):
        emit_error(
            f"Invalid skill name '{name}'. "
            f"Must match SAFE_NAME_RE: ^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,63}}$",
            json_output=False,
            code="INVALID_SKILL_NAME",
        )
        raise typer.Exit(1)

    # 2. Resolve target parent directory
    if target_dir is not None:
        target_parent = target_dir
    else:
        import os

        from agentos.skills.paths import resolve_skill_layer_dirs

        try:
            from agentos.gateway.config import GatewayConfig

            config = GatewayConfig.load(os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
            workspace_root = Path(config.workspace_dir) if config.workspace_dir else None
            workspace_override = (
                Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
            )
            allow_bundled = config.skills.allow_bundled
            managed_override = config.skills.managed_dir
            extra_dirs = [Path(d) for d in config.skills.extra_dirs]
        except Exception:
            workspace_root = None
            workspace_override = None
            allow_bundled = True
            managed_override = None
            extra_dirs = []

        layer_dirs = resolve_skill_layer_dirs(
            allow_bundled=allow_bundled,
            workspace_root=workspace_root,
            workspace_override=workspace_override,
            managed_override=managed_override,
            extra_dirs=extra_dirs,
        )

        candidates: list[Path] = []
        if layer_dirs.workspace_dir is not None:
            candidates.append(layer_dirs.workspace_dir)
        if layer_dirs.project_agents_dir is not None:
            candidates.append(layer_dirs.project_agents_dir)
        if layer_dirs.extra_dirs:
            candidates.extend(layer_dirs.extra_dirs)
        if layer_dirs.personal_agents_dir is not None:
            candidates.append(layer_dirs.personal_agents_dir)

        target_parent = None
        for candidate in candidates:
            if candidate.is_dir():
                target_parent = candidate
                break

        if target_parent is None:
            if workspace_override is not None:
                target_parent = workspace_override
            else:
                project_root = workspace_root if workspace_root is not None else Path.cwd()
                target_parent = project_root / "skills"

    # 3. Define target file paths
    skill_dir = target_parent / name
    skill_md = skill_dir / "SKILL.md"
    run_py = skill_dir / "scripts" / "run.py"

    # 4. Check for existing files
    if not force:
        if skill_md.exists():
            console.print(
                f"[red]Error:[/] File '{skill_md}' already exists. Use --force to overwrite."
            )
            raise typer.Exit(1)
        if with_script and run_py.exists():
            console.print(
                f"[red]Error:[/] File '{run_py}' already exists. Use --force to overwrite."
            )
            raise typer.Exit(1)

    # 5. Create directories
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        if with_script:
            (skill_dir / "scripts").mkdir(exist_ok=True)
    except Exception as exc:
        console.print(f"[red]Error:[/] Failed to create directory '{skill_dir}': {exc}")
        raise typer.Exit(1)

    # 6. Generate SKILL.md contents
    import yaml

    frontmatter = {
        "name": name,
        "description": description or "A custom skill template.",
        "always": False,
        "triggers": list(triggers or []),
        "provenance": {
            "origin": "local",
            "license": "Apache-2.0",
            "upstream_url": "",
            "maintained_by": "Local",
        },
        "metadata": {
            "agentos": {
                "emoji": "💡",
            }
        },
    }

    if with_script:
        frontmatter["entrypoint"] = {
            "command": "python {baseDir}/scripts/run.py",
            "args": [
                "--message",
                "{{ inputs.user_message }}",
            ],
            "parse": "json",
            "timeout": 30,
        }

    try:
        fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False)
    except Exception as exc:
        console.print(f"[red]Error:[/] Failed to serialize frontmatter: {exc}")
        raise typer.Exit(1)

    skill_md_content = f"""---
{fm_yaml}---

# {name.title()} Skill

A custom skill template initialized via `agentos skills init`.

## How it works

This skill packages task-specific instructions to guide the agent.
The instructions here are loaded by the agent to understand how to handle the prompt or invoke code.
"""

    if with_script:
        skill_md_content += """
### Script Execution

This skill includes an executable script `scripts/run.py` that is invoked by the agent.
The entrypoint configures the command:
```yaml
entrypoint:
  command: python {baseDir}/scripts/run.py
  args:
    - --message
    - "{{ inputs.user_message }}"
```
The script processes arguments, runs custom logic, and outputs a JSON response structure.
"""

    skill_md_content += """
### Declaring Dependencies

To declare binary or environment variables dependencies, edit the frontmatter block in this file
under `metadata`:
```yaml
# metadata:
#   requires:
#     bins: [curl]       # Binaries needed on PATH
#     env: [API_KEY]     # Environment variables needed
```
"""

    # 7. Generate scripts/run.py contents if requested
    run_py_content = """import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description="Custom skill script")
    parser.add_argument("--message", type=str, default="", help="Input message")
    args = parser.parse_args()

    # Custom script logic goes here
    result = {
        "status": "success",
        "message": f"Hello from custom script! Received: {args.message}",
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
"""

    # 8. Write files
    try:
        skill_md.write_text(skill_md_content, encoding="utf-8")
        if with_script:
            run_py.write_text(run_py_content, encoding="utf-8")
            console.print(
                f"[green]Initialized custom skill with script template:[/] {name} at {skill_dir}"
            )
        else:
            console.print(f"[green]Initialized custom skill template:[/] {name} at {skill_dir}")
    except Exception as exc:
        console.print(f"[red]Error writing skill template files:[/] {exc}")
        raise typer.Exit(1)
