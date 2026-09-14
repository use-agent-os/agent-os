"""``config set`` must accept a declared key whose value is currently null (#2031).

Both branches of ``config_set`` decide whether a key exists by walking
``GatewayConfig().to_toml_dict()``. That view is ``model_dump(exclude_none=True)``,
so every declared key whose current value is ``None`` is absent from it and
reads exactly like a typo:

    $ agentos config get auth.token --config config.toml
    auth.token = None
    $ agentos config set auth.token s3cr3t --config config.toml
    Key not found: auth.token          # exit 1

54 declared keys are in that state on a stock config. ``auth.token`` is the one
that bites: ``src/agentos/skills/bundled/agentos/SKILL.md`` tells the agent to
run ``config set auth.mode token`` followed by ``config set auth.token …``, and
only the first of those worked — leaving the gateway asking for a token it has
no way to store.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomli_w
from typer.testing import CliRunner

from agentos.cli.main import app
from agentos.gateway.config import GatewayConfig

runner = CliRunner()

# Declared keys that are None on a stock config, across nesting depths and
# value types, each with a value its own validator accepts.
_NULL_DEFAULT_KEYS = [
    ("workspace_strict", "true", True),
    ("auth.token", "s3cr3t", "s3cr3t"),
    ("auth.password", "hunter2", "hunter2"),
    ("auth.trusted_proxy", "10.0.0.1", "10.0.0.1"),
    ("attachments.media_root", "/srv/media", "/srv/media"),
    ("tools.profile", "minimal", "minimal"),
    ("task_runtime.turn_hard_deadline_s", "900", 900),
    ("memory.embedding.model", "text-embedding-3-small", "text-embedding-3-small"),
    ("memory.embedding.remote.base_url", "https://api.example.com", "https://api.example.com"),
]


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(tomli_w.dumps(GatewayConfig().to_toml_dict()), encoding="utf-8")
    return path


def _set(key: str, value: str, config_file: Path):
    return runner.invoke(app, ["config", "set", key, value, "--config", str(config_file)])


def _read(config_file: Path, key: str):
    node = tomllib.loads(config_file.read_text(encoding="utf-8"))
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ── A declared-but-null key is settable ────────────────────────────────────


@pytest.mark.parametrize(("key", "raw", "_stored"), _NULL_DEFAULT_KEYS, ids=lambda v: str(v))
def test_a_null_defaulted_key_can_be_set(
    key: str, raw: str, _stored: object, config_file: Path
) -> None:
    """Fails without the fix: 'Key not found', exit 1."""
    result = _set(key, raw, config_file)

    assert result.exit_code == 0, result.output
    assert "Key not found" not in result.output


@pytest.mark.parametrize(("key", "raw", "stored"), _NULL_DEFAULT_KEYS, ids=lambda v: str(v))
def test_the_value_reaches_the_file(key: str, raw: str, stored: object, config_file: Path) -> None:
    """Exit 0 is not enough — the TOML has to carry it to the next boot."""
    _set(key, raw, config_file)

    assert _read(config_file, key) == stored


def test_the_written_config_still_loads(config_file: Path) -> None:
    """A created key must not produce a file the gateway then refuses."""
    _set("auth.token", "s3cr3t", config_file)

    assert GatewayConfig.load(config_file).auth.token == "s3cr3t"


def test_enabling_gateway_auth_works_end_to_end(config_file: Path) -> None:
    """The documented pair from the bundled SKILL.md, in order.

    ``auth.mode`` survived ``exclude_none`` and always worked; ``auth.token``
    did not, so following the documentation left the gateway demanding a token
    it had no way to store.
    """
    assert _set("auth.mode", "token", config_file).exit_code == 0
    assert _set("auth.token", "s3cr3t", config_file).exit_code == 0

    loaded = GatewayConfig.load(config_file)
    assert loaded.auth.mode == "token"
    assert loaded.auth.token == "s3cr3t"


def test_the_env_var_branch_prints_the_export_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--config`` the command exists to print an export hint; the
    same check stopped it before reaching that line."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["config", "set", "auth.token", "s3cr3t"])

    assert result.exit_code == 0, result.output
    assert "AGENTOS_GATEWAY_AUTH__TOKEN" in result.output


def test_a_typed_value_is_still_parsed(config_file: Path) -> None:
    """Creation must not turn everything into a string."""
    assert _set("workspace_strict", "true", config_file).exit_code == 0

    assert _read(config_file, "workspace_strict") is True


def test_a_created_key_is_not_written_as_a_bare_string(config_file: Path) -> None:
    """An int-typed key keeps its type through the created path."""
    assert _set("task_runtime.turn_hard_deadline_s", "900", config_file).exit_code == 0

    assert _read(config_file, "task_runtime.turn_hard_deadline_s") == 900


# ── The typo guard must survive ───────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["auth.tokenn", "nope", "llm.nope.deeper", "auth.token.deeper"],
)
def test_an_unknown_key_is_still_rejected(key: str, config_file: Path) -> None:
    """Guard: passes either way by design. Widening what counts as a known key
    must not let a typo persist — including a path that walks *through* a
    null-valued leaf."""
    result = _set(key, "x", config_file)

    assert result.exit_code == 1
    assert "Key not found" in result.output


def test_an_unknown_key_is_still_rejected_on_the_env_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: passes either way by design."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["config", "set", "auth.tokenn", "x"])

    assert result.exit_code == 1
    assert "Key not found" in result.output


def test_an_invalid_value_is_still_refused(config_file: Path) -> None:
    """Guard: passes either way by design — model validation runs after the
    key check, and a created key must go through it like any other."""
    result = _set("task_runtime.turn_hard_deadline_s", '"not-a-number"', config_file)

    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_an_existing_non_null_key_still_works(config_file: Path) -> None:
    """Guard: passes either way by design."""
    assert _set("llm.provider", "openai", config_file).exit_code == 0
    assert _read(config_file, "llm.provider") == "openai"


def test_the_free_form_skills_map_still_works(config_file: Path) -> None:
    """Guard: passes either way by design — the ``skills.config.*`` creation
    path added for #834 must keep working alongside the declared-key one."""
    assert _set("skills.config.wiki.path", "/srv/wiki", config_file).exit_code == 0

    assert _read(config_file, "skills.config.wiki.path") == "/srv/wiki"
