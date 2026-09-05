"""Offline tests for the robinhood-rwa-addresses card-artifact renderer.

``rwa_cards.py`` turns a lookup result into the payload the Web chat renders as
an inline card grid (`application/vnd.agentos.cards+json`, frontend
`views/chat/transcript/cards.ts`). The contract that matters here is the badge
tone: an undeployed listing or an impersonator must not look like a verified
token at a glance.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/agentos/skills/bundled/robinhood-rwa-addresses/scripts/rwa_cards.py"
)

_spec = importlib.util.spec_from_file_location("rwa_cards", _SCRIPT)
assert _spec is not None and _spec.loader is not None
rwa_cards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwa_cards)


def _match(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": "Apple",
        "symbol": "AAPL",
        "address": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9",
        "chainId": 4663,
        "decimals": 18,
        "isStockToken": True,
        "logoURI": "https://assets.example/aapl.png",
        "status": "verified",
        "beacon": "0xe10b6f6b275de231345c20d14ab812db62151b00",
    }
    base.update(overrides)
    return base


def _result(*matches: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": "Apple", "matches": list(matches)}
    payload.update(extra)
    return payload


def test_mime_matches_the_frontend_renderer() -> None:
    """cards.ts keys off this exact string; a drift here silently degrades."""
    assert rwa_cards.CARDS_MIME == "application/vnd.agentos.cards+json"


def test_card_carries_symbol_name_logo_and_address() -> None:
    card = rwa_cards.build_cards(_result(_match()))[0]
    assert card["title"] == "AAPL"
    assert card["subtitle"] == "Apple"
    assert card["logo"] == "https://assets.example/aapl.png"
    assert {"label": "Address", "value": _match()["address"], "copyable": True} in card["fields"]
    assert {"label": "Chain", "value": "4663"} in card["fields"]


def test_status_drives_the_badge_tone() -> None:
    tones = {
        "verified": "positive",
        "not-deployed": "warning",
        "not-a-stock-token": "danger",
        "unverified": "neutral",
    }
    for status, tone in tones.items():
        card = rwa_cards.build_cards(_result(_match(status=status)))[0]
        assert card["badge"] == status
        assert card["badgeTone"] == tone, status


def test_unknown_status_degrades_to_neutral() -> None:
    """A future status must not crash the renderer or borrow a misleading colour."""
    card = rwa_cards.build_cards(_result(_match(status="brand-new")))[0]
    assert card["badgeTone"] == "neutral"


def test_warning_rides_along_as_the_subtitle() -> None:
    """The grid must never show an undeployed address without its caveat."""
    payload = rwa_cards.build_payload(
        _result(_match(status="not-deployed"), warning="not deployed: do not send funds")
    )
    assert payload["subtitle"] == "not deployed: do not send funds"


def test_title_names_the_query() -> None:
    assert rwa_cards.build_payload(_result(_match()))["title"] == "Robinhood Chain — Apple"
    assert rwa_cards.build_payload({"matches": []})["title"] == "Robinhood Chain"


def test_matches_without_an_identity_are_dropped() -> None:
    assert rwa_cards.build_cards(_result({"address": "0x1"})) == []
    assert rwa_cards.build_cards({"matches": ["not-an-object"]}) == []
    assert rwa_cards.build_cards({}) == []


def test_a_match_missing_optional_fields_still_renders() -> None:
    card = rwa_cards.build_cards(_result({"symbol": "GME"}))[0]
    assert card["title"] == "GME"
    assert card["logo"] == ""
    assert card["fields"] == []


def test_name_only_match_uses_the_name_as_the_title() -> None:
    card = rwa_cards.build_cards(_result({"name": "Apple"}))[0]
    assert card["title"] == "Apple"
    # No symbol, so the name is not repeated underneath itself.
    assert card["subtitle"] == ""


def test_match_order_is_preserved() -> None:
    """rwa_lookup already ranks verified matches first; do not reshuffle them."""
    cards = rwa_cards.build_cards(
        _result(
            _match(symbol="AAPL", status="verified"),
            _match(symbol="JPM", status="not-deployed"),
        )
    )
    assert [c["title"] for c in cards] == ["AAPL", "JPM"]


def test_rwa_cards_creates_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import json
    import sys

    out = tmp_path / "nested" / "dir" / "cards.json"
    payload = _result(_match())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["rwa_cards.py", "--output", str(out)])
    assert rwa_cards.main() == 0
    assert out.is_file()
