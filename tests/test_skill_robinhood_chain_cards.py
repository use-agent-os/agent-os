"""Offline tests for the robinhood-chain-stocks card renderer.

``chain_cards.py`` turns an on-chain reading into the payload the Web chat draws
as a card. The contract that matters is the badge: the two things a reader can
act on wrongly -- a **stale price** and an **unverified token** -- must surface
as the badge, not as a footnote under a green "verified".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/agentos/skills/bundled/robinhood-chain-stocks/scripts/chain_cards.py"
)

_spec = importlib.util.spec_from_file_location("chain_cards", _SCRIPT)
assert _spec is not None and _spec.loader is not None
chain_cards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chain_cards)


def _token(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "address": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9",
        "chainId": 4663,
        "onchainSymbol": "AAPL",
        "symbol": "AAPL",
        "name": "Apple",
        "decimals": 18,
        "totalSupplyFormatted": 12540.64790925,
        "isStockToken": True,
        "uiMultiplierFormatted": 1.0005660800610925,
        "oraclePaused": False,
        "price": {"usd": 326.68717647, "stale": False, "ageSeconds": 1712},
    }
    base.update(overrides)
    return base


def _result(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": "Apple", "chainId": 4663, "token": _token()}
    payload.update(overrides)
    return payload


def test_mime_matches_the_frontend_renderer() -> None:
    assert chain_cards.CARDS_MIME == "application/vnd.agentos.cards+json"


def test_card_carries_the_token_logo_when_the_list_had_one() -> None:
    card = chain_cards.build_cards(_result(token=_token(logoURI="https://a.example/n.png")))[0]
    assert card["logo"] == "https://a.example/n.png"


def test_a_token_without_a_logo_renders_an_empty_one() -> None:
    """The renderer drops an empty logo rather than leaving a torn frame."""
    assert chain_cards.build_cards(_result())[0]["logo"] == ""


def test_card_carries_price_supply_multiplier_and_address() -> None:
    card = chain_cards.build_cards(_result())[0]
    assert card["title"] == "AAPL"
    assert card["subtitle"] == "Apple"
    labels = [f["label"] for f in card["fields"]]
    assert labels == ["Price", "Supply", "Multiplier", "Address", "Chain"]
    assert card["fields"][0]["value"] == "$326.6872"
    address = next(f for f in card["fields"] if f["label"] == "Address")
    assert address["copyable"] is True


def test_a_healthy_reading_reads_verified() -> None:
    assert chain_cards.badge(_token()) == ("verified", "positive")


def test_a_stale_price_outranks_verified_on_the_badge() -> None:
    """Quoting a stale price as current is the misread this guards against."""
    token = _token(price={"usd": 326.0, "stale": True})
    assert chain_cards.badge(token) == ("price stale", "warning")


def test_a_paused_oracle_shows_as_a_warning() -> None:
    assert chain_cards.badge(_token(oraclePaused=True)) == ("oracle paused", "warning")


def test_a_confirmed_impersonator_reads_danger() -> None:
    assert chain_cards.badge(_token(isStockToken=False)) == ("not-a-stock-token", "danger")


def test_unreachable_chain_is_unverified_never_fake() -> None:
    """isStockToken is three-state: None means the check did not run."""
    assert chain_cards.badge(_token(isStockToken=None)) == ("unverified", "neutral")


def test_unverified_outranks_a_stale_price() -> None:
    """Whether the token is real at all matters more than how fresh the price is."""
    token = _token(isStockToken=None, price={"usd": 1.0, "stale": True})
    assert chain_cards.badge(token) == ("unverified", "neutral")


def test_a_non_positive_feed_answer_is_omitted_not_shown_as_zero() -> None:
    """The skill forbids reporting a non-positive answer as a price."""
    for price in ({"usd": 0, "stale": False}, {"usd": -1.5, "stale": False}, {}):
        card = chain_cards.build_cards(_result(token=_token(price=price)))[0]
        assert "Price" not in [f["label"] for f in card["fields"]]


def test_caveats_ride_along_as_the_subtitle() -> None:
    stale = chain_cards.build_payload(_result(token=_token(price={"usd": 1.0, "stale": True})))
    assert "stale" in stale["subtitle"]
    unreachable = chain_cards.build_payload(_result(token=_token(isStockToken=None)))
    assert "not disproven" in unreachable["subtitle"]
    assert chain_cards.build_payload(_result())["subtitle"] == ""


def test_title_names_the_query() -> None:
    assert chain_cards.build_payload(_result())["title"] == "Robinhood Chain — Apple"


def test_a_result_without_a_token_renders_nothing() -> None:
    assert chain_cards.build_cards({"query": "zzz"}) == []
    assert chain_cards.build_cards({"token": "not-an-object"}) == []
    assert chain_cards.build_cards({"token": {}}) == []


def test_onchain_symbol_is_used_when_the_list_symbol_is_missing() -> None:
    card = chain_cards.build_cards(_result(token=_token(symbol="", onchainSymbol="TSLA")))[0]
    assert card["title"] == "TSLA"


def test_main_creates_nested_parent_dirs_for_output(tmp_path: Path) -> None:
    """main() must create parent directories when the output path is nested.

    Without ``parent.mkdir(parents=True, exist_ok=True)``, ``Path.write_text``
    raises ``FileNotFoundError`` when the parent directory does not exist —
    the same gap fixed for the CLI cost exporter in PR #923.
    """
    import json
    import subprocess
    import sys

    nested = tmp_path / "nested" / "deep" / "cards.json"
    payload_in = _result()  # a valid chain_stocks object with a token

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--output", str(nested)],
        input=json.dumps(payload_in),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert nested.exists()
    written = json.loads(nested.read_text(encoding="utf-8"))
    assert written["type"] == "cards"
    assert len(written["cards"]) == 1
