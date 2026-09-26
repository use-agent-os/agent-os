"""``senior-unilp-manager`` reported a node fault as a simulation revert.

``simulate_call``'s ``eth_call`` fallback turned every exception into a
populated ``revert`` field, so ``lp_write``'s dry-run printed
``result: REVERTED`` and "Fix the parameters (or the approvals) and re-run"
for a rate-limited endpoint -- a protocol claim that never happened, and
advice to change parameters that were fine. Same class as #3253/#3309: a node
fault never names a revert.

These tests pin:

* a bare-string rate limit, a ``code 19`` transient error and an HTTP-level
  failure each print ``REFUSED`` (never ``REVERTED``) and exit 2;
* a real contract revert (``code 3`` + revert data) still prints
  ``REVERTED`` with the decoded reason -- the anti-drift case;
* ``RpcError.answered`` tells the two kinds apart.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "senior-unilp-manager"
    / "scripts"
)

# "not weth" as Error(string) revert data (the shape #3309's repro uses).
REVERT_DATA = "0x08c379a0" + "00" * 31 + "20" + "00" * 31 + "08" + "6e6f742077657468" + "00" * 24

CHAIN = {"positionManager": "0x000000000004444c5dc75cB358380D2e3dE08A90", "chainId": 4663}
SIGNER = {
    "address": "0x1111111111111111111111111111111111111111",
    "privateKey": "0x00",
    "simulateOnly": False,
}
CTX = {"title": "t", "rows": [["k", "v"]], "hashFields": {"a": 1}, "data": "0xdeadbeef", "value": 0}


def _load(name: str):
    entry = str(_SCRIPTS)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        return importlib.import_module(name)
    finally:
        if added:
            sys.path.remove(entry)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """``load_env()`` writes the real ``~/.agentos/.env`` into ``os.environ``; keep it out."""
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


class _NodeFaultClient:
    """A provider without ``eth_simulateV1`` whose ``eth_call`` raises *exc*."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def request(self, method: str, params=None):
        raise RuntimeError(f"{method}: the method does not exist/is not available")

    def call(self, to, data, block="latest", from_address=None):
        raise self._exc


def _run_plan(capsys, exc: Exception) -> tuple[int, str]:
    lp_write = _load("lp_write")
    with pytest.raises(SystemExit) as excinfo:
        lp_write.run_plan(_NodeFaultClient(exc), CHAIN, {}, SIGNER, dict(CTX))
    return excinfo.value.code, capsys.readouterr().out


NODE_FAULTS = [
    ("bare-string rate limit", lambda rpc: rpc.RpcError("eth_call", "rate limit exceeded")),
    (
        "code 19 transient error",
        lambda rpc: rpc.RpcError(
            "eth_call",
            {"code": 19, "message": "Temporary internal error. Please retry, trace-id: a4c4fd6c"},
        ),
    ),
    ("HTTP-level failure", lambda rpc: RuntimeError("eth_call: HTTP 429")),
]


@pytest.mark.parametrize("label,build", NODE_FAULTS, ids=[case[0] for case in NODE_FAULTS])
def test_a_node_fault_is_reported_as_refused_not_reverted(capsys, label, build):
    rpc = _load("unilp.rpc")
    code, out = _run_plan(capsys, build(rpc))

    assert code == 2
    assert "result   : REFUSED" in out
    assert "REVERTED" not in out
    assert "retry in a moment" in out
    assert "Fix the parameters" not in out


def test_a_real_revert_still_reports_reverted(capsys):
    rpc = _load("unilp.rpc")
    exc = rpc.RpcError(
        "eth_call", {"code": 3, "message": "execution reverted", "data": REVERT_DATA}
    )
    code, out = _run_plan(capsys, exc)

    assert code == 2
    assert "result   : REVERTED" in out
    assert "REFUSED" not in out
    assert "not weth" in out
    assert "Fix the parameters" in out


def test_answered_separates_a_revert_from_a_node_fault():
    rpc = _load("unilp.rpc")

    assert rpc.RpcError("eth_call", {"code": 3, "message": "execution reverted"}).answered
    assert rpc.RpcError("eth_call", {"code": -32000, "message": "execution reverted"}).answered
    assert not rpc.RpcError("eth_call", "rate limit exceeded").answered
    assert not rpc.RpcError(
        "eth_call", {"code": 19, "message": "Temporary internal error. Please retry"}
    ).answered
    assert not rpc.RpcError("eth_call", None).answered

    # A revert blob is the contract's own output, whatever the message says.
    assert rpc.RpcError(
        "eth_call", {"code": -32000, "message": "VM Exception", "data": REVERT_DATA}
    ).answered
    assert rpc.RpcError(
        "eth_call", {"code": -32000, "message": "VM Exception", "data": "0xdeadbeef"}
    ).answered


def test_a_revert_blob_under_a_vm_exception_message_still_reports_reverted(capsys):
    """A blob is contract output even when the message does not name the revert."""
    rpc = _load("unilp.rpc")
    exc = rpc.RpcError(
        "eth_call", {"code": -32000, "message": "VM Exception", "data": REVERT_DATA}
    )
    code, out = _run_plan(capsys, exc)

    assert code == 2
    assert "result   : REVERTED" in out
    assert "REFUSED" not in out
    assert "not weth" in out
