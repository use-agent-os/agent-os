"""Dependency installation for skills — brew, npm, go, uv, download.

Which kinds exist and what each one runs lives in
:mod:`agentos.skills.install_kinds`, shared with the agent tool and with
the display-only hints so the three can't drift apart again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from agentos.skills.install_kinds import (
    ARGV_INSTALL_KINDS,
    DOWNLOAD_URL_RE,
    MANUAL_INSTALL_KINDS,
    InstallSpecError,
    build_install_argv,
    is_supported_install_kind,
    normalize_install_kind,
    render_install_command,
)
from agentos.skills.types import SkillInstallSpec

log = structlog.get_logger(__name__)


@dataclass
class DepResult:
    """Result of installing a single dependency."""

    kind: str
    identifier: str
    success: bool
    message: str = ""


async def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    """Run a subprocess with timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return -1, "", "Timed out"
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def _install_via_argv(spec: SkillInstallSpec) -> DepResult:
    """Run the shared command for an argv-shaped install kind."""
    kind = normalize_install_kind(spec.kind)
    identifier = spec.formula or spec.package or spec.module or spec.id
    try:
        argv = build_install_argv(spec)
    except InstallSpecError as exc:
        return DepResult(kind=kind, identifier=identifier, success=False, message=str(exc))

    code, out, err = await _run(argv)
    if code == 0:
        return DepResult(kind=kind, identifier=identifier, success=True, message="Installed")
    return DepResult(kind=kind, identifier=identifier, success=False, message=err.strip()[:200])


async def install_download(spec: SkillInstallSpec) -> DepResult:
    """Download a binary from a URL."""
    import shutil
    from pathlib import Path

    url = spec.url
    if not url or not DOWNLOAD_URL_RE.match(url):
        return DepResult(
            kind="download", identifier=url or "", success=False, message=f"Invalid URL: {url}"
        )

    bin_name = spec.bins[0] if spec.bins else url.rsplit("/", 1)[-1]
    dest = Path.home() / ".local" / "bin" / bin_name

    code, out, err = await _run(["curl", "-fsSL", "-o", str(dest), url])
    if code != 0:
        return DepResult(kind="download", identifier=url, success=False, message=err.strip()[:200])

    dest.chmod(0o755)
    # Verify it landed on PATH
    if shutil.which(bin_name):
        return DepResult(
            kind="download", identifier=bin_name, success=True, message=f"Downloaded to {dest}"
        )
    return DepResult(
        kind="download",
        identifier=bin_name,
        success=True,
        message=f"Downloaded to {dest} (may need PATH update)",
    )


# Keyed by canonical kind — see agentos.skills.install_kinds.
_INSTALLERS = {
    **{kind: _install_via_argv for kind in ARGV_INSTALL_KINDS},
    "download": install_download,
}


async def install_deps(specs: list[SkillInstallSpec]) -> list[DepResult]:
    """Install all dependencies for a skill. Returns results per spec."""
    results = []
    for spec in specs:
        kind = normalize_install_kind(spec.kind)
        handler = _INSTALLERS.get(kind)
        if handler is None:
            if kind in MANUAL_INSTALL_KINDS:
                # Spell the command out: nothing else on this path shows it,
                # and telling the operator to go find it is a dead end.
                command = render_install_command(spec)
                message = f"Install kind '{kind}' needs elevated privileges"
                message += f" — run: {command}" if command else " and cannot be run here"
            elif is_supported_install_kind(kind):  # pragma: no cover - defensive
                message = f"No installer wired for kind: {kind}"
            else:
                message = f"Unsupported install kind: {spec.kind}"
            results.append(DepResult(kind=kind, identifier=spec.id, success=False, message=message))
            continue
        try:
            result = await handler(spec)
        except FileNotFoundError:
            result = DepResult(
                kind=kind,
                identifier=spec.id,
                success=False,
                message=f"Tool not found for kind '{kind}' (brew/npm/go/uv/curl)",
            )
        except Exception as exc:
            result = DepResult(
                kind=kind,
                identifier=spec.id,
                success=False,
                message=f"Error: {exc}",
            )
        results.append(result)
        log.info("deps.install", kind=kind, id=spec.id, success=result.success)
    return results
