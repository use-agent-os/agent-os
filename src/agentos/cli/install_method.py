"""Detect how the running ``agentos`` was installed and how to upgrade it.

Two independent hazards from the OpenClaw / Hermes case studies drive this
module:

* **Wrong upgrade channel** — running ``pip install -U`` against a uv-tool
  install silently no-ops (or corrupts the tool venv). We inspect where the
  running executable / package actually lives and pick the matching upgrade
  command, and for plain-pip / editable installs we refuse to fake it.

* **PATH gaps on macOS** (Hermes's #1 incident cluster) — the upgrade
  subprocess hangs or fails because ``uv`` / ``pipx`` is not on the ``PATH``
  the daemon inherited. We resolve the delegated tool to an ABSOLUTE path
  against an environment augmented with the standard login locations before
  spawning anything.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

DIST_NAME = "use-agent-os"

# The extras profile every managed upgrade installs. Mirrors install_source.sh's
# default (`--profile recommended`), so the two install paths agree on what a
# complete AgentOS is: without it the ONNX embedding models and the pilot router
# silently degrade.
UPGRADE_EXTRAS = "recommended"

# Standard login-shell locations that a GUI-launched or daemon-inherited
# environment frequently drops. Appended (not prepended) so an operator's own
# PATH ordering still wins.
_LOGIN_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


class InstallMethod(StrEnum):
    UV_TOOL = "uv-tool"
    PIPX = "pipx"
    PIP = "pip"
    EDITABLE = "editable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UpgradePlan:
    """How to upgrade the running install.

    ``delegated`` is True when AgentOS can run the upgrade itself (uv-tool /
    pipx). When False, ``command`` is the exact command the operator must run
    by hand and the ``upgrade`` command exits 3 rather than pretending.
    """

    method: InstallMethod
    delegated: bool
    tool: str | None
    command: list[str]
    manual_hint: str


def _package_location() -> Path:
    """Absolute path to the installed ``agentos`` package directory."""

    import agentos

    return Path(agentos.__file__).resolve().parent


def _looks_editable(pkg_dir: Path) -> bool:
    """True when the package is imported from a source checkout (editable/-e).

    An editable install lives in the project tree (``src/agentos``) rather than
    under a ``site-packages`` directory.
    """

    parts = pkg_dir.parts
    if "site-packages" in parts or "dist-packages" in parts:
        return False
    # ``src/agentos`` layout is the tell-tale of this repo's editable install.
    return pkg_dir.parent.name == "src" or (pkg_dir.parent / "pyproject.toml").exists()


def runtime_python_tag() -> str:
    """``major.minor`` of the interpreter currently running the CLI.

    The upgrade pins the rebuilt tool venv to this version so ``--force`` can
    never silently move a 3.13 install back onto whatever interpreter uv would
    have picked by default.
    """

    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _direct_url_payload(dist: str) -> dict[str, object] | None:
    """Parsed PEP 610 ``direct_url.json`` for ``dist``, or ``None``."""

    import importlib.metadata

    try:
        raw = importlib.metadata.distribution(dist).read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - missing/unreadable metadata is just "not a direct URL"
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def installed_from_directory(
    dist: str = DIST_NAME,
    *,
    package_dir: Path | None = None,
) -> Path | None:
    """Local directory this install was BUILT FROM, per PEP 610, else ``None``.

    Purely informational: ``agentos upgrade`` always installs the published
    release, and uses this only to tell a source installer where the door back
    to their checkout is. It must therefore never raise — every failure mode
    degrades to ``None``.

    Only ``dir_info`` counts. ``archive_info`` (a local ``.whl``/``.tar.gz``) and
    ``vcs_info`` (a ``git+https://`` install, whose clone is a uv cache artifact)
    are not checkouts the operator edits, so neither is reported.
    """

    payload = _direct_url_payload(dist)
    if payload is not None and isinstance(payload.get("dir_info"), dict):
        url = payload.get("url")
        if isinstance(url, str) and url.startswith("file:"):
            # url2pathname handles percent-encoding and Windows drive letters;
            # slicing the URL would corrupt both.
            path = urllib.parse.urlparse(url).path
            try:
                return Path(urllib.request.url2pathname(path))
            except (OSError, ValueError):
                return None
        return None

    # An editable install laid down without usable metadata (``uv sync`` in the
    # tree, ``pip install -e`` on older tooling) still has the ``src/agentos``
    # tell that _looks_editable keys on.
    pkg_dir = package_dir if package_dir is not None else _package_location()
    if pkg_dir.parent.name == "src" and len(pkg_dir.parents) > 1:
        return pkg_dir.parents[1]
    return None


def _under(path: Path, *needles: str) -> bool:
    lowered = [part.lower() for part in path.parts]
    return any(needle in lowered for needle in needles)


def _uv_tools_roots(env: dict[str, str]) -> list[Path]:
    """Resolved uv-tools root directories to prefix-match against.

    ``UV_TOOL_DIR`` overrides the default ``~/.local/share/uv/tools`` location
    entirely (uv honours it), so a custom value must be treated as a first-class
    tools root — otherwise a uv-tool install under it is misclassified and we
    hand back the actively-wrong ``pip`` suggestion (a uv tool venv has no pip).
    """

    roots: list[Path] = []
    override = env.get("UV_TOOL_DIR", "").strip()
    if override:
        try:
            roots.append(Path(override).resolve())
        except OSError:
            pass
    return roots


def _is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or lives beneath it (both resolved)."""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def detect_install_method(
    *,
    executable: str | None = None,
    package_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> InstallMethod:
    """Classify the running install.

    ``executable`` / ``package_dir`` / ``env`` are injectable for tests; they
    default to ``sys.executable``, the real ``agentos`` package location, and
    ``os.environ``.
    """

    environ = env if env is not None else dict(os.environ)
    raw_exe = Path(executable or sys.executable)
    exe = raw_exe.resolve()
    pkg_dir = package_dir if package_dir is not None else _package_location()

    # Editable / source checkout first: it can otherwise masquerade as pip.
    if _looks_editable(pkg_dir):
        return InstallMethod.EDITABLE

    # A custom UV_TOOL_DIR relocates the whole tools tree; the executable may
    # even be a symlink from a bin dir INTO that tree, so check both the raw path
    # and its symlink-resolved target against the override root.
    uv_roots = _uv_tools_roots(environ)
    if uv_roots:
        candidates: list[Path] = []
        for candidate in (raw_exe, exe, pkg_dir):
            candidates.append(candidate)
            try:
                candidates.append(candidate.resolve())
            except OSError:
                pass
        if any(_is_within(c, root) for c in candidates for root in uv_roots):
            return InstallMethod.UV_TOOL

    # uv tool installs live under the uv tools root, e.g.
    #   ~/.local/share/uv/tools/use-agent-os/...
    #   $XDG_DATA_HOME/uv/tools/...
    for candidate in (exe, pkg_dir):
        parts = [p.lower() for p in candidate.parts]
        if "uv" in parts and "tools" in parts:
            return InstallMethod.UV_TOOL

    # pipx venvs: ~/.local/share/pipx/venvs/<name>/ or $PIPX_HOME/venvs/...
    for candidate in (exe, pkg_dir):
        if _under(candidate, "pipx"):
            return InstallMethod.PIPX

    if "site-packages" in [p.lower() for p in pkg_dir.parts] or "dist-packages" in [
        p.lower() for p in pkg_dir.parts
    ]:
        return InstallMethod.PIP

    return InstallMethod.UNKNOWN


def hardened_path_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env copy whose ``PATH`` includes the standard login dirs.

    The augmented directories are *appended* so an operator's own ordering is
    preserved; only genuinely-missing login locations are added.
    """

    env = dict(base_env if base_env is not None else os.environ)

    # On Windows, environment variable names are case-insensitive. If base_env
    # had "Path" or "path", resolve it to avoid duplicate keys and preserve
    # the operator's existing PATH entries.
    existing_paths: list[str] = []
    keys_to_remove: list[str] = []
    for k, v in env.items():
        if (sys.platform == "win32" and k.upper() == "PATH") or k == "PATH":
            if v:
                existing_paths.append(v)
            if k != "PATH":
                keys_to_remove.append(k)
    for k in keys_to_remove:
        del env[k]

    current = os.pathsep.join(existing_paths)
    entries = [p for p in current.split(os.pathsep) if p]
    seen = set(entries)
    home = env.get("HOME") or env.get("USERPROFILE") or str(Path.home())
    login_dirs = list(_LOGIN_PATH_DIRS) + [str(Path(home) / ".local" / "bin")]
    for extra in login_dirs:
        if extra not in seen:
            entries.append(extra)
            seen.add(extra)
    env["PATH"] = os.pathsep.join(entries)
    return env


def resolve_tool(tool: str, env: dict[str, str] | None = None) -> str | None:
    """Resolve ``tool`` (``uv`` / ``pipx``) to an ABSOLUTE path.

    Uses a PATH-hardened environment so a daemon-inherited PATH missing
    ``/opt/homebrew/bin`` etc. still finds the tool. Returns ``None`` when the
    tool genuinely cannot be found.
    """

    hardened = hardened_path_env(env)
    resolved = shutil.which(tool, path=hardened.get("PATH"))
    if resolved:
        return str(Path(resolved).resolve())
    return None


def build_upgrade_plan(
    *,
    method: InstallMethod | None = None,
    env: dict[str, str] | None = None,
    dist: str = DIST_NAME,
    python_tag: str | None = None,
) -> UpgradePlan:
    """Build the :class:`UpgradePlan` for the running install."""

    resolved_method = method if method is not None else detect_install_method()
    python = python_tag if python_tag is not None else runtime_python_tag()
    spec = f"{dist}[{UPGRADE_EXTRAS}]"

    if resolved_method is InstallMethod.UV_TOOL:
        # ``install`` rather than ``upgrade``, and this is the whole point of the
        # command: ``uv tool upgrade`` takes only a bare tool NAME and re-resolves
        # whatever uv's receipt recorded. When AgentOS was installed from a local
        # checkout (install_source.sh passes ``.``), that receipt is a DIRECTORY,
        # so ``upgrade`` rebuilds the wheel from the working tree and never
        # touches PyPI — silently re-packaging whatever
        # ``src/agentos/gateway/static/dist/`` happens to be on disk, because
        # nothing here runs ``npm run build``. ``uv tool install <dist>[extras]``
        # moves the install onto the published release, whose wheel carries a
        # CI-built Control UI (pypi-publish.yml builds and verify-archive's it).
        #
        # ``--force`` is required: without it uv no-ops on an already-installed
        # tool, which is the same silent-no-op class of bug. ``--python`` pins the
        # rebuilt venv to the interpreter already in use so the forced reinstall
        # cannot move a 3.13 install onto another version.
        uv = resolve_tool("uv", env)
        uv_argv = ["tool", "install", "--force", "--python", python, spec]
        uv_hint = f'uv tool install --force --python {python} "{spec}"'
        if uv is not None:
            return UpgradePlan(
                method=resolved_method,
                delegated=True,
                tool=uv,
                command=[uv, *uv_argv],
                manual_hint=uv_hint,
            )
        return UpgradePlan(
            method=resolved_method,
            delegated=False,
            tool=None,
            command=["uv", *uv_argv],
            manual_hint=uv_hint,
        )

    if resolved_method is InstallMethod.PIPX:
        # Same reasoning as uv: ``pipx upgrade`` re-resolves the recorded spec, so
        # a pipx install laid down from a local path keeps rebuilding from that
        # path. ``pipx install --force <dist>[extras]`` rebuilds the managed venv
        # from the published release instead.
        pipx = resolve_tool("pipx", env)
        pipx_argv = ["install", "--force", spec]
        pipx_hint = f'pipx install --force "{spec}"'
        if pipx is not None:
            return UpgradePlan(
                method=resolved_method,
                delegated=True,
                tool=pipx,
                command=[pipx, *pipx_argv],
                manual_hint=pipx_hint,
            )
        return UpgradePlan(
            method=resolved_method,
            delegated=False,
            tool=None,
            command=["pipx", *pipx_argv],
            manual_hint=pipx_hint,
        )

    if resolved_method is InstallMethod.EDITABLE:
        # An editable install serves the Control UI straight out of the checkout,
        # so the reinstall that matters is the source one — and only
        # install_source.sh rebuilds the browser bundle before installing.
        return UpgradePlan(
            method=resolved_method,
            delegated=False,
            tool=None,
            command=["git", "pull"],
            manual_hint=(
                "editable / source checkout — pull and reinstall from the checkout: "
                "git pull && bash scripts/install_source.sh"
            ),
        )

    if resolved_method is InstallMethod.PIP:
        # A genuine site-packages install: pip is the right upgrade tool. The
        # extras must be spelled out — pip drops any that are not in the spec.
        pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", spec]
        return UpgradePlan(
            method=resolved_method,
            delegated=False,
            tool=None,
            command=pip_cmd,
            manual_hint=f'{sys.executable} -m pip install --upgrade "{spec}"',
        )

    # UNKNOWN: we could not classify the install, so a blind ``python -m pip``
    # may be actively wrong (e.g. a uv/pipx venv has no pip). List all three
    # installers and let the operator pick the one they originally used.
    return UpgradePlan(
        method=resolved_method,
        delegated=False,
        tool=None,
        command=[sys.executable, "-m", "pip", "install", "--upgrade", spec],
        manual_hint=(
            "could not detect the install method — reinstall/upgrade with your "
            f'original installer, e.g.:\n    uv tool install --force "{spec}"\n    '
            f'pipx install --force "{spec}"\n    '
            f'{sys.executable} -m pip install --upgrade "{spec}"'
        ),
    )
