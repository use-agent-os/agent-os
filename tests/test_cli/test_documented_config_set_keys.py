"""Every ``agentos config set`` example in the docs must run when followed.

`docs/cli.md` and `README.product.md` both documented
``agentos config set gateway.port 18791``. There is no ``[gateway]`` table —
the listen port is top-level ``port`` on ``GatewayConfig``, which is
``extra = forbid`` — so the copy-pasted command exited 1 with ``Key not found``
instead of changing the port
(https://github.com/use-agent-os/agent-os/issues/840).

The guard runs each documented example through the CLI rather than pinning the
one bad string, so the next wrong key fails here instead of in a user's
terminal.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

ROOT = Path(__file__).resolve().parents[2]
# The two files issue #840 names, plus the bundled SKILL.md the agent itself
# reads. That third file was excluded while `config set auth.token` exited 1 —
# `to_toml_dict()` omits a key whose value is null, so `_set_key` read a
# declared key as a typo (#2031). With that fixed the exclusion is gone and the
# agent-facing examples are guarded like the human-facing ones.
DOCS = ("docs/cli.md", "README.product.md", "src/agentos/skills/bundled/agentos/SKILL.md")

# Matches anywhere, so a fenced block, an inline `agentos config set x.y` in
# prose and a `$`-prefixed shell line are all covered. Stops at the end of the
# line; a placeholder such as `config set <dot.key>` is skipped below.
_CONFIG_SET = re.compile(r"agentos config set ([^\n`]+)")

runner = CliRunner()


def _documented_commands() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for rel in DOCS:
        path = ROOT / rel
        if not path.is_file():
            # Do not raise at collection time: that would also disable the
            # meta-guard below, which exists to catch exactly this drift.
            found.append((rel, ""))
            continue
        for raw in _CONFIG_SET.findall(path.read_text(encoding="utf-8")):
            args = raw.strip().rstrip(".")
            # `<dot.key>`-style placeholders are prose, not runnable examples.
            if args.startswith("<"):
                continue
            # SKILL.md annotates its examples with a trailing `# comment`, which
            # is shell syntax the reader's shell would drop before running it.
            try:
                tokens = shlex.split(args, comments=True)
            except ValueError:
                # Unbalanced quotes: prose, not a runnable example.
                continue
            if not tokens:
                continue
            found.append((rel, shlex.join(tokens)))
    return found


def test_every_documented_file_exists_and_has_examples() -> None:
    """Guard the scanner: a missing file or zero matches must not pass silently."""
    for rel in DOCS:
        assert (ROOT / rel).is_file(), f"{rel} moved — update DOCS in this test"
    assert any(args for _, args in _documented_commands()), (
        "no `agentos config set` examples found — the regex is stale"
    )


@pytest.mark.parametrize(("doc", "args"), _documented_commands(), ids=lambda v: v or "<missing>")
def test_documented_config_set_command_runs(doc: str, args: str, tmp_path, monkeypatch) -> None:
    assert args, f"{doc} is missing or unreadable"
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    argv = shlex.split(args)
    # An example that names its own `--config` target is pointed at a tmp file
    # so the test never touches the developer's real config.
    if "--config" in argv:
        argv[argv.index("--config") + 1] = str(tmp_path / "config.toml")

    result = runner.invoke(app, ["config", "set", *argv])

    assert result.exit_code == 0, (
        f"{doc} documents `agentos config set {args}`, but it exits "
        f"{result.exit_code}:\n{result.output}"
    )
    assert "Key not found" not in result.output
