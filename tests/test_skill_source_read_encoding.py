"""Issue #2335: bundled skill scripts read text files with no explicit encoding.

``Path.read_text()``, ``Path.write_text()`` and ``open()`` with no ``encoding``
use the platform default, which on Windows is the console code page rather than
UTF-8. The files being read are UTF-8, so the result depends on the operator's
locale:

===========================  ==========================================
code page                    result
===========================  ==========================================
UTF-8 (POSIX, ``PYTHONUTF8``)  correct
cp1252 (Western Windows)       decodes, silently wrong (mojibake)
cp936 / cp932 (CJK Windows)    ``UnicodeDecodeError``
===========================  ==========================================

The worst instance is ``poolsdotfun-token-launcher/scripts/selftest.py``, whose
Tier 7 block reads its own sources to assert *capability separation* — "the read
path must not be able to sign. Structure, not convention." On a CJK code page it
raises before the first check runs, so the security property is unverified and
the failure names a codec rather than a capability.

The locale cannot be simulated in-process: CPython resolves the default encoding
in C, and patching ``locale.getencoding`` / ``getpreferredencoding`` does not
reach it. So the tests here prove the fix is load-bearing a different way — by
asserting the bytes genuinely cannot be decoded as cp932/cp936, and decode
*differently* under cp1252 — and then assert the absence of bare text I/O
structurally, which is what stops the class returning.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src" / "agentos" / "skills"
BUNDLED = SKILLS / "bundled"

#: The files this change fixes, and the call kinds each one had.
FIXED = (
    "poolsdotfun-token-launcher/scripts/selftest.py",
    "senior-unilp-manager/scripts/selftest.py",
    "gmgn-wallet-analysis/scripts/analyze.py",
)

#: Sources the poolsdotfun Tier 7 capability-separation block reads.
TIER7_SOURCES = (
    "poolsdotfun-token-launcher/scripts/pools_read.py",
    "poolsdotfun-token-launcher/scripts/poolsfun/plan.py",
    "poolsdotfun-token-launcher/scripts/pools_write.py",
)


def unencoded_text_io(path: Path) -> list[tuple[int, str]]:
    """``read_text`` / ``write_text`` / text-mode ``open`` with no ``encoding``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {kw.arg for kw in node.keywords}
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "read_text",
            "write_text",
        }:
            if "encoding" not in keywords:
                sites.append((node.lineno, node.func.attr))
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if "b" in mode:
                continue  # binary needs no encoding
            if "encoding" not in keywords:
                sites.append((node.lineno, "open"))
    return sorted(sites)


# ── the fix, per file ───────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", FIXED, ids=[r.split("/")[0] for r in FIXED])
def test_the_fixed_file_has_no_unencoded_text_io(rel: str) -> None:
    sites = unencoded_text_io(BUNDLED / rel)

    assert sites == [], f"{rel} still reads or writes text without an encoding at {sites}"


def test_no_bundled_or_experimental_skill_reads_text_without_an_encoding() -> None:
    """The regression guard, and the reason this is worth more than three edits.

    Sweeps every skill script rather than the three the issue named, so the next
    one fails here instead of on a CJK operator's machine.
    """
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SKILLS.rglob("*.py")):
        sites = unencoded_text_io(path)
        if sites:
            offenders[path.relative_to(SKILLS).as_posix()] = sites

    assert offenders == {}, f"skill scripts with unencoded text I/O: {offenders}"


# ── the fix is load-bearing, not cosmetic ───────────────────────────────────


@pytest.mark.parametrize("rel", TIER7_SOURCES, ids=lambda r: Path(r).stem)
def test_the_sources_tier7_reads_actually_contain_non_ascii(rel: str) -> None:
    """If these were ASCII the fix would be a no-op, so pin that they are not."""
    raw = (BUNDLED / rel).read_bytes()

    assert any(byte > 0x7F for byte in raw), f"{rel} is pure ASCII; this fix would be vacuous"


@pytest.mark.parametrize("rel", TIER7_SOURCES, ids=lambda r: Path(r).stem)
@pytest.mark.parametrize("code_page", ["cp932", "cp936"])
def test_a_cjk_code_page_cannot_decode_these_sources_at_all(rel: str, code_page: str) -> None:
    """This is the crash: on a CJK Windows box the locale default is this codec,
    and Tier 7 dies before its first ``check(...)``."""
    raw = (BUNDLED / rel).read_bytes()

    with pytest.raises(UnicodeDecodeError):
        raw.decode(code_page)


@pytest.mark.parametrize("rel", TIER7_SOURCES, ids=lambda r: Path(r).stem)
def test_cp1252_decodes_but_silently_differs(rel: str) -> None:
    """The quieter half: a Western Windows box does not crash, it just reads
    something other than what is on disk."""
    raw = (BUNDLED / rel).read_bytes()

    assert raw.decode("cp1252") != raw.decode("utf-8")


@pytest.mark.parametrize("rel", TIER7_SOURCES, ids=lambda r: Path(r).stem)
def test_an_explicit_utf8_read_matches_the_bytes_on_disk(rel: str) -> None:
    """Newline-normalised, because ``read_text`` translates CRLF and
    ``read_bytes`` does not — that difference is not an encoding problem."""
    path = BUNDLED / rel

    decoded = path.read_text(encoding="utf-8")
    raw = path.read_bytes().decode("utf-8").replace("\r\n", "\n")

    assert decoded == raw


# ── Tier 7 still checks what it checked ─────────────────────────────────────


def test_tier7_still_finds_the_markers_it_asserts_on() -> None:
    """The fix changes how the text is decoded, not what is in it. These are the
    exact substrings the capability-separation checks search for.
    """
    scripts = BUNDLED / "poolsdotfun-token-launcher" / "scripts"

    read_src = (scripts / "pools_read.py").read_text(encoding="utf-8")
    write_src = (scripts / "pools_write.py").read_text(encoding="utf-8")

    # The read path must not be able to sign.
    assert "resolve_private_key(" not in read_src
    # The write path's confirmation gate is counted, so it must still be found.
    assert write_src.count("_confirmed(") > write_src.count("def _confirmed(")
    # And the flag Tier 9 asserts is absent.
    assert "private-key" not in write_src


# ── round-trip, so the write side is covered too ────────────────────────────


def test_non_ascii_round_trips_through_the_patched_write_and_read(tmp_path: Path) -> None:
    """``senior-unilp-manager``'s selftest writes as well as reads. A bare
    ``write_text`` raises on a code page that cannot hold the content, which is
    the same defect facing the other way.
    """
    payload = '{"note": "日本語 café — em dash"}'
    path = tmp_path / "mandate.json"

    path.write_text(payload, encoding="utf-8")

    assert path.read_text(encoding="utf-8") == payload
    assert path.read_bytes() == payload.encode("utf-8")


def test_a_json_fixture_with_non_ascii_loads(tmp_path: Path) -> None:
    """``gmgn-wallet-analysis`` reads a caller-supplied fixture, and that skill
    has an explicit ``zh`` mode — so non-ASCII in the fixture is expected input,
    not an edge case."""
    import json

    fixture = tmp_path / "f.json"
    fixture.write_text(
        json.dumps({"_wallet": "钱包", "_chain": "sol", "_gaps": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    with open(fixture, encoding="utf-8") as handle:
        loaded = json.load(handle)

    assert loaded["_wallet"] == "钱包"
