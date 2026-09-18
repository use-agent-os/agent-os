"""Issue #2564: ``agentos dist`` was listed but never documented.

``docs/cli.md`` and ``docs/operations.md`` now describe the command: stdout by
default, ``--output`` / ``-o`` to write a file, the six payload keys, and the
reproducibility and secret-hygiene contract. Documentation drifts unless
something holds it to the code, so half of this file pins the two pages to
``dist_cmd`` and ``workspace_state`` -- every key the builder emits must be
named in the docs, the flag must be spelled as it is declared, the schema
version must match -- and the other half checks that every behavioural claim
the docs make is true of the command as it runs.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli.dist_cmd import app as dist_app
from agentos.cli.main import app
from agentos.dist.workspace_state import SCHEMA_VERSION, build_workspace_state, to_json

DOCS = Path(__file__).resolve().parents[2] / "docs"
CLI_MD = (DOCS / "cli.md").read_text(encoding="utf-8")
OPERATIONS_MD = (DOCS / "operations.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of a ``## heading`` up to the next ``## ``."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match is not None, f"no '## {heading}' section"
    return match.group(1)


CLI_SECTION = _section(CLI_MD, "Install Inventory")
OPS_SECTION = _section(OPERATIONS_MD, "Install Inventory")


# ── the docs are held to the code ───────────────────────────────────────────


def test_every_payload_key_is_documented_in_cli_md() -> None:
    for key in build_workspace_state():
        assert f"`{key}`" in CLI_SECTION, key


def test_no_documented_key_has_been_removed_from_the_payload() -> None:
    """The other direction: a key the docs describe must still exist."""
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", CLI_SECTION, re.M))

    assert documented == set(build_workspace_state())


def test_the_documented_schema_version_matches_the_code() -> None:
    assert f"(currently `{SCHEMA_VERSION}`)" in CLI_SECTION


def test_the_flag_is_documented_as_declared() -> None:
    """``--output`` and ``-o`` come from the Typer option on ``dist``."""
    result = CliRunner().invoke(dist_app, ["--help"])

    assert result.exit_code == 0
    assert "--output" in result.output and "-o" in result.output
    assert "`--output` / `-o PATH`" in CLI_SECTION
    assert "--output" in OPS_SECTION


def test_the_documented_gateway_defaults_match_the_code() -> None:
    defaults = build_workspace_state()["gateway_defaults"]
    assert isinstance(defaults, dict)

    for key, value in defaults.items():
        assert f"`{key}`" in CLI_SECTION, key
        assert f"`{value}`" in CLI_SECTION, value


def test_the_documented_python_requirement_matches_the_code() -> None:
    assert f"`{build_workspace_state()['python_requires']}`" in CLI_SECTION


def test_the_summary_table_row_still_exists() -> None:
    assert "| `agentos dist` |" in CLI_MD


def test_the_two_pages_cross_link_each_other() -> None:
    assert "operations.md#install-inventory" in CLI_SECTION
    assert "cli.md#install-inventory" in OPS_SECTION


def test_the_contract_the_docs_promise_is_the_one_the_module_states() -> None:
    """The module docstring is the source of the reproducibility and
    secret-hygiene contract; the docs must not promise more or less."""
    from agentos.dist import workspace_state

    module_doc = workspace_state.__doc__ or ""
    assert "byte-identical" in module_doc and "byte-identical" in CLI_SECTION
    for word in ("timestamp", "environment", "path"):
        assert word in module_doc.lower() and word in CLI_SECTION.lower(), word


# ── the claims the docs make are true of the command ────────────────────────


def test_with_no_flags_the_payload_goes_to_stdout_and_nothing_else() -> None:
    result = CliRunner().invoke(app, ["dist"])

    assert result.exit_code == 0
    assert json.loads(result.output) == build_workspace_state()
    assert result.output == to_json(), "exactly the payload: no path, no banner"


def test_output_writes_the_file_and_prints_its_path(tmp_path: Path) -> None:
    target = tmp_path / "workspace-state.json"

    result = CliRunner().invoke(app, ["dist", "--output", str(target)])

    assert result.exit_code == 0
    assert result.output.strip() == str(target)
    assert json.loads(target.read_text(encoding="utf-8")) == build_workspace_state()


def test_the_short_flag_is_the_same_as_the_long_one(tmp_path: Path) -> None:
    long_target = tmp_path / "long.json"
    short_target = tmp_path / "short.json"

    CliRunner().invoke(app, ["dist", "--output", str(long_target)])
    CliRunner().invoke(app, ["dist", "-o", str(short_target)])

    assert long_target.read_bytes() == short_target.read_bytes()


def test_output_creates_missing_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "support" / "2026-09" / "workspace-state.json"

    result = CliRunner().invoke(app, ["dist", "-o", str(target)])

    assert result.exit_code == 0
    assert target.is_file()


def test_the_file_written_is_byte_identical_to_stdout(tmp_path: Path) -> None:
    """``agentos dist > f`` and ``agentos dist -o f`` must give the same file."""
    target = tmp_path / "f.json"
    stdout = CliRunner().invoke(app, ["dist"]).output
    CliRunner().invoke(app, ["dist", "-o", str(target)])

    assert target.read_text(encoding="utf-8") == stdout


def test_two_runs_are_byte_identical() -> None:
    first = CliRunner().invoke(app, ["dist"]).output
    second = CliRunner().invoke(app, ["dist"]).output

    assert first == second


def test_the_payload_ignores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docs say no value comes from the environment; make it lie if it can."""
    before = CliRunner().invoke(app, ["dist"]).output
    monkeypatch.setenv("AGENTOS_GATEWAY_PORT", "1")
    monkeypatch.setenv("AGENTOS_LLM_API_KEY", "sk-should-never-appear-0000000000")
    monkeypatch.setenv("HOME", "/nowhere")

    after = CliRunner().invoke(app, ["dist"]).output

    assert after == before
    assert "sk-should-never-appear" not in after


def test_the_payload_carries_no_paths_timestamps_or_credential_keys() -> None:
    payload = to_json()

    assert os.sep not in payload.replace("\\n", "") or "/" not in payload
    assert not re.search(r"\d{4}-\d{2}-\d{2}", payload), "no dates"
    for key in build_workspace_state():
        assert not key.endswith(("_key", "_token", "_secret")), key


def test_the_payload_is_sorted_and_indented_as_documented() -> None:
    payload = to_json()

    assert payload == json.dumps(json.loads(payload), sort_keys=True, indent=2) + "\n"


def test_lists_are_sorted_so_two_installs_diff_cleanly() -> None:
    state = build_workspace_state()

    for key in ("bundled_channels", "bundled_tools"):
        values = state[key]
        assert isinstance(values, list)
        assert values == sorted(values), key


def test_pipeline_friendly_output_ends_with_exactly_one_newline() -> None:
    output = CliRunner().invoke(app, ["dist"]).output

    assert output.endswith("}\n") and not output.endswith("\n\n")


def test_the_examples_in_the_docs_are_the_commands_that_run() -> None:
    """Every ``agentos dist …`` line shown in either page parses and exits 0."""
    shown = set(re.findall(r"^agentos dist(?:[ \t]+[^#\n]*)?", CLI_SECTION + OPS_SECTION, re.M))
    assert shown, "the docs show at least one invocation"

    for line in shown:
        args = line.split()[1:]
        args = [a if not a.endswith(".json") else str(Path(a).name) for a in args]
        with CliRunner().isolated_filesystem():
            result = CliRunner().invoke(app, args)
            assert result.exit_code == 0, line
