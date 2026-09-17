from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "poolsdotfun-token-launcher"
    / "scripts"
    / "pools_write.py"
)


def _load_module():
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("pools_write", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeRpcClient:
    def read(self, *args, **kwargs):
        return 0


def test_cmd_approve_amount_zero_revokes_allowance_and_includes_flag(capsys, monkeypatch) -> None:
    monkeypatch.setenv("POOLSFUN_PRIVATE_KEY", "0x" + "1" * 64)
    module = _load_module()
    client = FakeRpcClient()

    args = {
        "_": ["approve"],
        "paired": "weth",
        "amount": "0",
        "broadcast": False,
        "confirm": None,
    }

    module.cmd_approve(client, args)

    out = capsys.readouterr().out
    assert "new allowance     : 0" in out
    assert "unlimited" not in out
    assert "--amount 0" in out or "--amount '0'" in out


def test_cmd_approve_amount_explicit_includes_flag(capsys, monkeypatch) -> None:
    monkeypatch.setenv("POOLSFUN_PRIVATE_KEY", "0x" + "1" * 64)
    module = _load_module()
    client = FakeRpcClient()

    args = {
        "_": ["approve"],
        "paired": "weth",
        "amount": "10",
        "broadcast": False,
        "confirm": None,
    }

    module.cmd_approve(client, args)

    out = capsys.readouterr().out
    assert "new allowance     : 10" in out
    assert "unlimited" not in out
    assert "--amount 10" in out or "--amount '10'" in out


def test_cmd_approve_amount_omitted_defaults_to_unlimited(capsys, monkeypatch) -> None:
    monkeypatch.setenv("POOLSFUN_PRIVATE_KEY", "0x" + "1" * 64)
    module = _load_module()
    client = FakeRpcClient()

    args = {
        "_": ["approve"],
        "paired": "weth",
        "broadcast": False,
        "confirm": None,
    }

    module.cmd_approve(client, args)

    out = capsys.readouterr().out
    assert "unlimited" in out
    assert "--amount" not in out
