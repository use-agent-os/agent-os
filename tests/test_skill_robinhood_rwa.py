"""Offline regression tests for the robinhood-rwa-addresses lookup script.

Two behaviours are under test:

* **Discovery** -- the skill's entrypoint defaults ``--query`` to the raw user
  message, so the matcher must resolve tokens from full sentences ("what is
  Apple's ticker?", "mã cổ phiếu Apple là gì") as well as bare names/tickers.
* **Verification** -- the CoinGecko index is not authoritative. It truncates
  long names (dropping the "Robinhood Token" marker) and lists assets Robinhood
  has announced but never deployed, so the on-chain beacon check is what decides
  whether an address is real. Everything here is offline: RPC results are fed in
  as fixtures.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src/agentos/skills/bundled/robinhood-rwa-addresses/scripts/rwa_lookup.py"
)

_spec = importlib.util.spec_from_file_location("rwa_lookup", _SCRIPT)
assert _spec is not None and _spec.loader is not None
rwa_lookup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwa_lookup)

_TOKENS = [
    {
        "chainId": 4663,
        "address": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9",
        "name": "Apple • Robinhood Token",
        "symbol": "AAPL",
        "decimals": 18,
        "logoURI": "https://assets.example/aapl.png",
    },
    {
        "chainId": 4663,
        "address": "0x322f0929c4625ed5bad873c95208d54e1c003b2d",
        "name": "Tesla • Robinhood Token",
        "symbol": "TSLA",
        "decimals": 18,
        "logoURI": "",
    },
    {
        "chainId": 4663,
        "address": "0x39ec44bee4f6a116c6f9b8de566848a985c53c60",
        "name": "e.l.f. Beauty • Robinhood Token",
        "symbol": "ELF",
        "decimals": 18,
        "logoURI": "",
    },
    {
        "chainId": 4663,
        "address": "0x1b0e319c6a659f002271b69db8a7df2f911c153e",
        "name": "GameStop • Robinhood Token",
        "symbol": "GME",
        "decimals": 18,
        "logoURI": "",
    },
    # Robinhood Chain is permissionless: this community token reuses GameStop's
    # name and ticker. Only the on-chain beacon reliably tells the two apart.
    {
        "chainId": 4663,
        "address": "0x7e86381a763f0ecca2bdf27c54eac403ddd48123",
        "name": "GameStop",
        "symbol": "GME",
        "decimals": 18,
        "logoURI": "",
    },
    # CoinGecko caps `name` at 60 characters, so long listings lose the
    # "• Robinhood Token" marker mid-word. These are real Stock Tokens.
    {
        "chainId": 4663,
        "address": "0x980dcf6766fa79f5cf0c4aadb3ab477ff15a9619",
        "name": "International Business Machines Corporation • Robinhood Toke",
        "symbol": "IBM",
        "decimals": 18,
        "logoURI": "",
    },
    {
        "chainId": 4663,
        "address": "0x15cd20759ce7f3285c29a319de2d1a2e098c6f43",
        "name": "State Street Technology Select Sector SPDR ETF • Robinhood T",
        "symbol": "XLK",
        "decimals": 18,
        "logoURI": "",
    },
    # Announced by Robinhood and carried by the index, but no contract on chain.
    {
        "chainId": 4663,
        "address": "0x07c44da0848960bff894f17584db8b2f60b2409e",
        "name": "JPMorgan Chase & Co. • Robinhood Token",
        "symbol": "JPM",
        "decimals": 18,
        "logoURI": "",
    },
]

_REAL_GME = "0x1b0e319c6a659f002271b69db8a7df2f911c153e"
_FAKE_GME = "0x7e86381a763f0ecca2bdf27c54eac403ddd48123"
_IBM = "0x980dcf6766fa79f5cf0c4aadb3ab477ff15a9619"
_JPM = "0x07c44da0848960bff894f17584db8b2f60b2409e"

_BEACON_WORD = "0x000000000000000000000000e10b6f6b275de231345c20d14ab812db62151b00"
_ZERO_WORD = "0x" + "0" * 64
_SOME_CODE = "0x6080604052"


def _symbols(query: str) -> list[str]:
    return [m["symbol"] for m in rwa_lookup.lookup(query, _TOKENS, limit=3)]


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_bare_name_and_ticker_resolve() -> None:
    assert _symbols("Apple") == ["AAPL"]
    assert _symbols("AAPL") == ["AAPL"]
    assert _symbols("tesla") == ["TSLA"]


def test_full_sentence_queries_resolve() -> None:
    # The entrypoint feeds the raw user message by default.
    assert _symbols("What is Apple's ticker?")[:1] == ["AAPL"]
    assert _symbols("mã cổ phiếu apple là gì")[:1] == ["AAPL"]
    assert _symbols("Robinhood contract address for Tesla")[:1] == ["TSLA"]


def test_ticker_inside_sentence_resolves() -> None:
    assert _symbols("gia AAPL bao nhieu")[:1] == ["AAPL"]


def test_no_match_returns_empty() -> None:
    assert _symbols("zzz-not-a-company") == []


def test_robinhood_suffix_stripped_from_names() -> None:
    matches = rwa_lookup.lookup("Apple", _TOKENS, limit=1)
    assert matches[0]["name"] == "Apple"
    assert matches[0]["address"] == "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9"


def test_community_token_impersonating_a_listing_is_excluded() -> None:
    """A lookalike must never be offered as the answer to a company question."""
    for query in ("GME", "GameStop", "mã cổ phiếu GME là gì"):
        matches = rwa_lookup.lookup(query, _TOKENS, limit=5)
        assert [m["address"] for m in matches] == [_REAL_GME], query


def test_include_community_widens_the_search_but_keeps_stock_tokens_first() -> None:
    matches = rwa_lookup.lookup("GME", _TOKENS, limit=5, include_community=True)
    assert [m["address"] for m in matches] == [_REAL_GME, _FAKE_GME]
    assert [m["isStockToken"] for m in matches] == [True, False]


def test_matches_are_tagged_with_stock_token_status() -> None:
    assert rwa_lookup.lookup("Apple", _TOKENS, limit=1)[0]["isStockToken"] is True


# --------------------------------------------------------------------------
# Truncated names (CoinGecko caps `name` at 60 chars)
# --------------------------------------------------------------------------


def test_stock_tokens_with_truncated_suffix_are_still_recognised() -> None:
    """The 60-character cap chops "• Robinhood Token" mid-word on long names."""
    assert rwa_lookup.is_stock_token({"name": _TOKENS[5]["name"]}) is True  # "… Robinhood Toke"
    assert rwa_lookup.is_stock_token({"name": _TOKENS[6]["name"]}) is True  # "… Robinhood T"


def test_truncated_listings_are_findable_by_ticker_and_name() -> None:
    """Regression: IBM and XLK used to return zero matches."""
    assert _symbols("IBM") == ["IBM"]
    assert _symbols("XLK") == ["XLK"]
    assert _symbols("International Business Machines")[:1] == ["IBM"]


def test_truncated_name_is_cleaned_for_display() -> None:
    match = rwa_lookup.lookup("IBM", _TOKENS, limit=1)[0]
    assert match["name"] == "International Business Machines Corporation"


def test_suffix_hint_does_not_fire_on_community_tokens_naming_robinhood() -> None:
    """The loose marker must not turn a memecoin into a "Stock Token"."""
    for name in (
        "GameStop",
        "Robinhood Wrapped ETH (Robinhood Chain)",
        "Turbo on Robinhood",
        "CAPTAIN ROBINHOOD",
        "Robinhood Payments Protocol",
        "RobinhoodCat",
    ):
        assert rwa_lookup.is_stock_token({"name": name}) is False, name
    assert rwa_lookup.is_stock_token({}) is False


# --------------------------------------------------------------------------
# On-chain verification
# --------------------------------------------------------------------------


def test_classify_reads_the_three_on_chain_outcomes() -> None:
    assert rwa_lookup.classify(_BEACON_WORD, _SOME_CODE) == rwa_lookup.STATUS_VERIFIED
    # A contract exists but points at some other beacon (or none): impersonator.
    assert rwa_lookup.classify(_ZERO_WORD, _SOME_CODE) == rwa_lookup.STATUS_NOT_STOCK
    # Nothing deployed at the address the index advertises.
    assert rwa_lookup.classify(_ZERO_WORD, "0x") == rwa_lookup.STATUS_NOT_DEPLOYED


def test_classify_reports_unreachable_rpc_as_unverified_not_fake() -> None:
    """A network fault must never be reported as proof that a token is fake."""
    assert rwa_lookup.classify(None, None) == rwa_lookup.STATUS_UNVERIFIED
    assert rwa_lookup.classify(_BEACON_WORD, None) == rwa_lookup.STATUS_UNVERIFIED
    assert rwa_lookup.classify(None, _SOME_CODE) == rwa_lookup.STATUS_UNVERIFIED


def _verify(matches: list[dict[str, Any]], results: dict[str, str], monkeypatch: Any) -> None:
    monkeypatch.setattr(rwa_lookup, "_rpc_batch", lambda *a, **k: results)
    rwa_lookup.verify_tokens(matches, "http://rpc.invalid", 5.0)


def test_verify_tokens_marks_a_genuine_stock_token(monkeypatch: Any) -> None:
    matches = rwa_lookup.lookup("Apple", _TOKENS, limit=1)
    _verify(matches, {"s0": _BEACON_WORD, "c0": _SOME_CODE}, monkeypatch)
    assert matches[0]["status"] == rwa_lookup.STATUS_VERIFIED
    assert matches[0]["beacon"] == rwa_lookup.ROBINHOOD_BEACON


def test_verify_tokens_flags_an_undeployed_listing(monkeypatch: Any) -> None:
    """Regression: JPM's advertised address holds no contract at all."""
    matches = rwa_lookup.lookup("JPM", _TOKENS, limit=1)
    assert matches[0]["address"] == _JPM
    _verify(matches, {"s0": _ZERO_WORD, "c0": "0x"}, monkeypatch)
    assert matches[0]["status"] == rwa_lookup.STATUS_NOT_DEPLOYED
    assert matches[0]["beacon"] is None


def test_verify_tokens_separates_the_real_and_fake_gme(monkeypatch: Any) -> None:
    matches = rwa_lookup.lookup("GME", _TOKENS, limit=5, include_community=True)
    _verify(
        matches,
        {"s0": _BEACON_WORD, "c0": _SOME_CODE, "s1": _ZERO_WORD, "c1": _SOME_CODE},
        monkeypatch,
    )
    by_address = {m["address"]: m["status"] for m in matches}
    assert by_address[_REAL_GME] == rwa_lookup.STATUS_VERIFIED
    assert by_address[_FAKE_GME] == rwa_lookup.STATUS_NOT_STOCK


def test_verify_tokens_degrades_to_unverified_when_the_rpc_fails(monkeypatch: Any) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise OSError("connection refused")

    monkeypatch.setattr(rwa_lookup, "_rpc_batch", _boom)
    matches = rwa_lookup.lookup("Apple", _TOKENS, limit=1)
    rwa_lookup.verify_tokens(matches, "http://rpc.invalid", 5.0)
    assert matches[0]["status"] == rwa_lookup.STATUS_UNVERIFIED
    assert matches[0]["beacon"] is None


def test_verified_matches_outrank_unverified_ones() -> None:
    ordered = rwa_lookup.sort_by_status(
        [
            {"symbol": "A", "status": rwa_lookup.STATUS_NOT_STOCK},
            {"symbol": "B", "status": rwa_lookup.STATUS_NOT_DEPLOYED},
            {"symbol": "C", "status": rwa_lookup.STATUS_VERIFIED},
            {"symbol": "D", "status": rwa_lookup.STATUS_UNVERIFIED},
        ]
    )
    assert [m["symbol"] for m in ordered] == ["C", "D", "B", "A"]


# --------------------------------------------------------------------------
# Caller-facing warnings
# --------------------------------------------------------------------------


def test_no_warning_when_a_verified_token_is_present() -> None:
    assert _warning([rwa_lookup.STATUS_VERIFIED, rwa_lookup.STATUS_NOT_STOCK]) is None


def test_undeployed_warning_tells_the_user_not_to_send_funds() -> None:
    warning = _warning([rwa_lookup.STATUS_NOT_DEPLOYED])
    assert warning is not None
    assert "not deployed" in warning
    assert "Do not send funds" in warning


def test_impersonator_warning_names_the_risk() -> None:
    warning = _warning([rwa_lookup.STATUS_NOT_STOCK])
    assert warning is not None
    assert "impersonator" in warning


def test_unreachable_warning_says_unverified_not_disproven() -> None:
    warning = _warning([rwa_lookup.STATUS_UNVERIFIED])
    assert warning is not None
    assert "UNVERIFIED" in warning
    assert "not disproven" in warning


def test_skipped_verification_is_not_reported_as_a_network_fault() -> None:
    """--no-verify is an operator choice, not an outage; the wording must differ."""
    warning = rwa_lookup._warning_for(
        [{"status": rwa_lookup.STATUS_UNVERIFIED}], verification_skipped=True
    )
    assert warning is not None
    assert "--no-verify" in warning
    assert "could not be reached" not in warning


def _warning(statuses: list[str]) -> str | None:
    return rwa_lookup._warning_for([{"status": s} for s in statuses])


# --------------------------------------------------------------------------
# --rpc-url validation
# --------------------------------------------------------------------------


def test_validate_http_url_rejects_non_http_schemes() -> None:
    """file://, ftp://, gopher://, javascript: are all rejected."""
    for invalid in [
        "file:///etc/passwd",
        "file:///c:/windows/system32/drivers/etc/hosts",
        "ftp://rpc.example.com",
        "gopher://example.com",
        "javascript:alert(1)",
    ]:
        with pytest.raises(ValueError, match="must be http:// or https://"):
            rwa_lookup._validate_http_url(invalid)
    for empty in ["", "   "]:
        with pytest.raises(ValueError, match="empty URL"):
            rwa_lookup._validate_http_url(empty)
    assert rwa_lookup._validate_http_url("http://127.0.0.1:8545") == "http://127.0.0.1:8545"
    assert (
        rwa_lookup._validate_http_url("https://rpc.mainnet.chain.robinhood.com")
        == "https://rpc.mainnet.chain.robinhood.com"
    )


def test_validate_http_url_rejects_missing_host() -> None:
    with pytest.raises(ValueError, match="missing host"):
        rwa_lookup._validate_http_url("http://")


def test_validate_http_url_accepts_valid_variants() -> None:
    assert rwa_lookup._validate_http_url("http://example.com") == "http://example.com"
    assert (
        rwa_lookup._validate_http_url("https://user:pass@host.com:8545")
        == "https://user:pass@host.com:8545"
    )
    assert rwa_lookup._validate_http_url("http://[::1]:7545") == "http://[::1]:7545"
    assert rwa_lookup._validate_http_url(rwa_lookup.DEFAULT_RPC_URL)


def test_main_rejects_invalid_rpc_url(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    code = rwa_lookup.main(["--query", "AAPL", "--rpc-url", "file:///etc/passwd"])
    assert code == 0
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert "invalid rpc-url" in payload.get("error", "")
    assert "must be http:// or https://" in payload.get("error", "")


def test_main_rejects_missing_host_url(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    code = rwa_lookup.main(["--query", "AAPL", "--rpc-url", "http://"])
    assert code == 0
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert "missing host" in payload.get("error", "")
