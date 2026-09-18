#!/usr/bin/env python3
"""Offline selftest — no network, no key, no funds.

Everything asserted here is pinned against something external and immutable: the
reference launch transaction, the deployed factory's error selectors, or a
property of the code's own structure. That is the point — a test that only checks
the code against itself would pass just as happily after a refactor broke the
calldata encoding, and the first sign of trouble would be a reverted launch.

Run it after any change:

    python3 selftest.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, actual, expected=None, *, truthy: bool = False) -> None:
    global CHECKS
    CHECKS += 1
    ok = bool(actual) if truthy else actual == expected
    if not ok:
        detail = f"expected {expected!r}, got {actual!r}" if not truthy else "falsy"
        FAILURES.append(f"{label}: {detail}")


def raises(label: str, fn, *, contains: str = "") -> None:
    global CHECKS
    CHECKS += 1
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 — asserting the failure itself
        if contains and contains.lower() not in str(exc).lower():
            FAILURES.append(f"{label}: raised, but message lacks {contains!r} — {exc}")
        return
    FAILURES.append(f"{label}: expected a raise, got none")


def section(title: str) -> None:
    print(f"\n── {title}")


# ── Tier 1: the reference launch ────────────────────────────────────────────
# The single most valuable assertion in this file. If `launch` calldata encoding
# ever drifts, this catches it offline instead of on-chain.
REF_TX_INPUT = (
    "0xce61a35c"
    "0000000000000000000000000000000000000000000000000000000000000160"
    "00000000000000000000000000000000000000000000000000000000000001a0"
    "00000000000000000000000000000000000000000000000000000000000001e0"
    "0000000000000000000000000000000000000000000000000000000000000007"
    "0000000000000000000000000bd7d308f8e1639fab988df18a8011f41eacad73"
    "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffd1778"
    "000000000000000000000000000000000000000000000000000000006a7d4cb4"
    "0000000000000000000000008a86e3927bd9e4200bc18dad3a158caa4806ba51"
    "0000000000000000000000008a86e3927bd9e4200bc18dad3a158caa4806ba51"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000009"
    "506f6f6c732046756e0000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "504f4f4c00000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000042"
    "697066733a2f2f6261666b72656961766a75786f796b35796f676c6b73626c35"
    "677479326d6b6737346673616f366732626c72356d6d6a66337772326b796f73"
    "6d61000000000000000000000000000000000000000000000000000000000000"
)
REF_DEPLOYER = "0x8a86E3927BD9E4200BC18DAD3A158CAa4806Ba51"
REF_METADATA = "ipfs://bafkreiavjuxoyk5yoglksbl5gty2mkg74fsao6g2blr5mmjf3wr2kyosma"
REF_TOKEN = "0x0762a4F683f0531b70bC7D6882781457d80F689a"


def tier1_reference_calldata() -> None:
    section("Tier 1 — reference transaction")
    from poolsfun.chains import WETH
    from poolsfun.factory import encode_launch, salt_hex

    encoded = encode_launch(
        "Pools Fun", "POOL", REF_METADATA, salt_hex(7), WETH, -190600,
        1786596532, REF_DEPLOYER, REF_DEPLOYER, 0, 0)
    check("launch calldata matches tx 0x0978ee72…", encoded.lower(), REF_TX_INPUT.lower())
    check("selector is 0xce61a35c", encoded[:10], "0xce61a35c")

    # int24 negative encoding is the easiest thing to get subtly wrong.
    check("int24 -190600 sign-extends",
          encoded[10 + 64 * 5:10 + 64 * 6],
          "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffd1778")


def tier2_create2() -> None:
    """CREATE2 prediction, reproduced locally from the factory's own formula."""
    section("Tier 2 — CREATE2 address derivation")
    from poolsfun.factory import salt_hex
    from poolsfun.keccak import keccak256_bytes

    # effectiveSalt = keccak256(abi.encodePacked(deployer, salt))
    packed = bytes.fromhex(REF_DEPLOYER[2:]) + bytes.fromhex(salt_hex(7)[2:])
    effective = keccak256_bytes(packed)
    check("effectiveSalt is 32 bytes", len(effective), 32)
    check("effectiveSalt is deterministic", effective, keccak256_bytes(packed))
    # A different deployer must land in a different namespace.
    other = keccak256_bytes(bytes.fromhex("00" * 20) + bytes.fromhex(salt_hex(7)[2:]))
    check("salt is namespaced by deployer", effective != other, True)

    from poolsfun.chains import USDG, WETH
    from poolsfun.factory import sorts_below
    check("reference token sorts below WETH", sorts_below(REF_TOKEN, WETH), True)
    check("WETH does not sort below the reference token",
          sorts_below(WETH, REF_TOKEN), False)
    check("USDG hit rate is the larger one",
          int(USDG, 16) > int(WETH, 16), True)


def tier3_error_selectors() -> None:
    """Every custom error, recomputed from its signature."""
    section("Tier 3 — revert decoding")
    from poolsfun.factory import ERROR_MESSAGES, explain_revert
    from poolsfun.keccak import function_selector

    expected = {
        "DeployFailed()": "0xb4f54111",
        "StartTickChanged()": "0x0f5ddbb1",
        "TokenNotToken0()": "0xd79cce06",
        "Expired()": "0x203d82d8",
        "CreatorNotCaller()": "0x7607bc0d",
        "AmbiguousDevBuy()": "0x6e4e2579",
        "DevBuyWethOnly()": "0xb6dc33e4",
        "DevBuyTooLittle()": "0xedaf7b53",
        "PairedAssetNotAllowed()": "0x5a9b44ca",
        "Paused()": "0x9e87fac8",
        "PoolAlreadyInitialized()": "0x7983c051",
    }
    for signature, selector in expected.items():
        check(f"selector {signature}", function_selector(signature), selector)
        check(f"{signature} has a message", selector in ERROR_MESSAGES, True)
        check(f"{signature} message is actionable",
              len(ERROR_MESSAGES[selector]) > 40, True)

    # Iterate the whole table, not just the hand-listed subset above. An entry
    # exempt from verification is exactly where a wrong selector hides — an
    # earlier revision carried three, one of which collided with ZeroAmount()
    # and so reported a completely different error with full confidence.
    from poolsfun.factory import ERROR_SIGNATURES

    check("every message is reachable by its computed selector",
          all(ERROR_MESSAGES[function_selector(sig)] is msg
              for sig, msg in ERROR_SIGNATURES.items()), True)
    check("no two errors collide on a selector",
          len(ERROR_MESSAGES), len(ERROR_SIGNATURES))
    check("every signature is well formed",
          all(s.endswith("()") and s[0].isupper() for s in ERROR_SIGNATURES), True)
    # The selectors that used to be wrong, pinned to the right owners.
    check("0x1f2a2005 is ZeroAmount, not DevBuyTooLarge",
          function_selector("ZeroAmount()"), "0x1f2a2005")
    check("ZeroAmount is not claimed by this factory's table",
          "0x1f2a2005" in ERROR_MESSAGES, False)
    check("DevBuyTooLarge selector", function_selector("DevBuyTooLarge()"), "0x82ce2bbd")
    check("LockerUnset selector", function_selector("LockerUnset()"), "0xf619c36a")
    check("InvalidTick selector", function_selector("InvalidTick()"), "0xce8ef7fc")

    check("TokenLaunched topic0",
          __import__("poolsfun.factory", fromlist=["x"]).TOPIC_TOKEN_LAUNCHED,
          "0xd1844be5e646143a1c9e6841471e58911bac843c7d033e435d304cfeba2c2153")

    # Error(string) from a dependency must still decode.
    payload = ("0x08c379a0" + "0" * 62 + "20" + "0" * 62 + "08"
               + "6e6f742077657468".ljust(64, "0"))
    check("Error(string) decodes", explain_revert(payload), "reverted: not weth")
    check("unknown selector yields None", explain_revert("0xdeadbeef"), None)
    check("non-hex yields None", explain_revert("nope"), None)


def tier4_curve_math() -> None:
    section("Tier 4 — curve math and tick bounds")
    from poolsfun.factory import (
        MAX_USABLE_TICK,
        TICK_SPACING,
        TOTAL_SUPPLY,
        fdv_usd,
        price_from_tick,
        validate_start_tick,
    )

    check("TOTAL_SUPPLY", TOTAL_SUPPLY, 10**27)
    check("MAX_USABLE_TICK", MAX_USABLE_TICK, 887200)
    check("MAX_USABLE_TICK is spacing-aligned", MAX_USABLE_TICK % TICK_SPACING, 0)

    # At the reference tick and ETH price, FDV lands on the $10k target.
    fdv = fdv_usd(-190600, 1891.508)
    check("reference FDV within 1% of $10k", 9900 < fdv < 10100, True)
    check("price is monotonic in tick",
          price_from_tick(-190400) > price_from_tick(-190600), True)

    validate_start_tick(-190600)
    validate_start_tick(0)
    raises("rejects unaligned tick", lambda: validate_start_tick(-190601),
           contains="multiple of 200")
    raises("rejects tick at the upper bound",
           lambda: validate_start_tick(MAX_USABLE_TICK), contains="outside")
    raises("rejects tick below the lower bound",
           lambda: validate_start_tick(-MAX_USABLE_TICK - 200), contains="outside")


def tier5_plan_hash() -> None:
    """The confirmation token must move for anything that changes the outcome."""
    section("Tier 5 — PLAN_HASH sensitivity")
    from poolsfun.chains import USDG, WETH
    from poolsfun.plan import action_hash, plan_hash, verify_plan_unchanged

    base = {
        "name": "My Token", "symbol": "MYT", "metadataUri": "ipfs://a",
        "salt": "0x" + "00" * 31 + "07", "pairedAsset": WETH,
        "expectedStartTick": -190600, "creator": REF_DEPLOYER,
        "feeRecipient": REF_DEPLOYER, "devBuyAmountIn": 0,
        "devBuyValueWei": 10**15, "devBuyMinOut": 123, "chainId": 4663,
        "factory": "0x626C3d09B65bF5d1D40E0D5F25e19fa49783B3D4",
    }
    digest = plan_hash(base)
    check("hash is 4 bytes", len(digest), 10)
    check("hash is stable", plan_hash(dict(base)), digest)
    check("hash is case-insensitive for addresses",
          plan_hash({**base, "creator": REF_DEPLOYER.lower()}), digest)

    for field, value in [
        ("name", "Other"), ("symbol", "OTH"), ("metadataUri", "ipfs://b"),
        ("salt", "0x" + "00" * 31 + "08"), ("pairedAsset", USDG),
        ("expectedStartTick", -190400), ("creator", "0x" + "11" * 20),
        ("feeRecipient", "0x" + "22" * 20), ("devBuyAmountIn", 1),
        ("devBuyValueWei", 10**15 + 1), ("devBuyMinOut", 124),
        ("chainId", 8453), ("factory", "0x" + "33" * 20),
    ]:
        check(f"hash changes with {field}",
              plan_hash({**base, field: value}) != digest, True)

    # Fields outside the commitment must NOT move it — otherwise every plan
    # expires the moment the clock ticks.
    for field, value in [("deadline", 999), ("gasPrice", 1), ("devBuyOut", 5)]:
        check(f"hash ignores {field}", plan_hash({**base, field: value}), digest)

    verify_plan_unchanged(base, digest)
    verify_plan_unchanged(base, digest.upper())
    raises("rejects a stale confirmation",
           lambda: verify_plan_unchanged({**base, "devBuyMinOut": 999}, digest),
           contains="plan changed")

    # Non-launch actions get their own namespace, and the action is part of it.
    token = REF_TOKEN
    fields = {"token": token, "caller": REF_DEPLOYER, "chainId": 4663}
    check("collect != claim", action_hash("collect", fields)
          != action_hash("claim", fields), True)
    check("action hash is stable",
          action_hash("collect", dict(fields)), action_hash("collect", fields))


def tier6_metadata() -> None:
    """Pinata is optional. This tier is the regression guard for that."""
    section("Tier 6 — metadata and the optional Pinata path")
    saved = os.environ.pop("PINATA_JWT", None)
    try:
        import poolsfun.chains as chains
        chains._env_loaded = True  # do not let a stray .env re-inject a JWT
        from poolsfun.metadata import (
            _guess_mime,
            _multipart,
            build_metadata,
            pinata_configured,
            resolve_metadata_uri,
            to_data_uri,
        )

        check("no JWT means not configured", pinata_configured(), False)

        uri, doc, how = resolve_metadata_uri(name="My Token", symbol="MYT",
                                             description="hi")
        check("image-less launch works with no secret", how, "inline (no image)")
        check("data URI scheme", uri.startswith("data:application/json;base64,"), True)
        decoded = json.loads(base64.b64decode(uri.split(",", 1)[1]))
        check("inline doc round-trips",
              decoded, {"name": "My Token", "symbol": "MYT", "description": "hi"})
        check("empty fields are omitted, not nulled", "image" in decoded, False)

        check("pass-through is untouched",
              resolve_metadata_uri(name="A", symbol="B",
                                   metadata_uri="ipfs://x")[0], "ipfs://x")
        for scheme in ("ipfs://x", "https://x", "data:application/json,{}"):
            resolve_metadata_uri(name="A", symbol="B", metadata_uri=scheme)
        raises("rejects a bad URI scheme",
               lambda: resolve_metadata_uri(name="A", symbol="B",
                                            metadata_uri="ftp://x"),
               contains="must start with")

        # The critical one: --image without a JWT must fail loudly, never
        # silently launch an image-less token with an immutable identity.
        png = Path(tempfile.mkdtemp()) / "logo.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        raises("--image without PINATA_JWT refuses",
               lambda: resolve_metadata_uri(name="A", symbol="B", image=str(png)),
               contains="PINATA_JWT")
        raises("the refusal names the escape hatch",
               lambda: resolve_metadata_uri(name="A", symbol="B", image=str(png)),
               contains="drop --image")
        raises("--pin-metadata without PINATA_JWT refuses",
               lambda: resolve_metadata_uri(name="A", symbol="B", pin=True),
               contains="PINATA_JWT")

        check("build_metadata field order",
              list(build_metadata("n", "s", description="d", image="i",
                                  website="w", twitter="t")),
              ["name", "symbol", "description", "image", "website", "twitter"])
        raises("oversized inline metadata is refused",
               lambda: to_data_uri(build_metadata("n", "s", description="x" * 9000)),
               contains="inline metadata")

        # Webchat attachments often have a wrong or missing extension.
        check("sniffs PNG past a wrong extension",
              _guess_mime(Path("a.bin"), b"\x89PNG\r\n\x1a\n"), "image/png")
        check("sniffs JPEG", _guess_mime(Path("a.bin"), b"\xff\xd8\xff\xe0"), "image/jpeg")
        check("falls back to the extension", _guess_mime(Path("a.webp"), b"???"),
              "image/webp")

        # Attachment discovery. AgentOS never surfaces a path, so this is the
        # only way a skill can turn "the image I just sent" into a file.
        from poolsfun.metadata import agentos_media_root, find_attachment_images, sniff_image_mime

        saved_root = os.environ.pop("AGENTOS_ATTACHMENTS_MEDIA_ROOT", None)
        saved_state = os.environ.pop("AGENTOS_STATE_DIR", None)
        try:
            check("media root defaults under ~/.agentos",
                  str(agentos_media_root()).endswith("/.agentos/media"), True)
            os.environ["AGENTOS_STATE_DIR"] = "/tmp/xyz"
            check("AGENTOS_STATE_DIR relocates it",
                  str(agentos_media_root()), "/tmp/xyz/media")
            os.environ["AGENTOS_ATTACHMENTS_MEDIA_ROOT"] = "/tmp/explicit"
            check("explicit media root wins", str(agentos_media_root()), "/tmp/explicit")
        finally:
            os.environ.pop("AGENTOS_ATTACHMENTS_MEDIA_ROOT", None)
            os.environ.pop("AGENTOS_STATE_DIR", None)
            if saved_root:
                os.environ["AGENTOS_ATTACHMENTS_MEDIA_ROOT"] = saved_root
            if saved_state:
                os.environ["AGENTOS_STATE_DIR"] = saved_state

        # Staged attachments have no extension, so the type must come from the
        # bytes. A sha256-named PNG must still be recognised as a PNG.
        root = Path(tempfile.mkdtemp())
        staged = root / "transcripts" / "sess-1"
        staged.mkdir(parents=True)
        (staged / ("a" * 64)).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        (staged / ("b" * 64)).write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
        (staged / ("c" * 64)).write_bytes(b"just some text, not an image")
        check("sniffs an extensionless PNG",
              sniff_image_mime(staged / ("a" * 64)), "image/png")
        check("non-image returns None", sniff_image_mime(staged / ("c" * 64)), None)
        check("missing file returns None", sniff_image_mime(staged / "nope"), None)

        # The transcript is the authority. Build a miniature sessions.db with a
        # recent inline attachment and a much older staged one from a different
        # session, and assert the recent one wins — the disk scan gets this
        # exactly backwards, which is how an unrelated 3-day-old blob was once
        # offered as a token logo.
        import sqlite3

        from poolsfun.metadata import find_chat_images, materialize_chat_image

        db = root / "sessions.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE transcript_entries "
                    "(id INTEGER PRIMARY KEY, session_key TEXT, role TEXT, "
                    " content TEXT, created_at INTEGER)")
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x01" * 64).decode()
        con.execute(
            "INSERT INTO transcript_entries VALUES (1,'agent:main:webchat:old','user',?,?)",
            (json.dumps({"text": "old", "attachments": [
                {"sha256_ref": "f" * 64, "name": "old.png",
                 "mime": "image/png", "size": 999}]}), 1_000_000_000_000))
        con.execute(
            "INSERT INTO transcript_entries VALUES (2,'agent:main:webchat:new','user',?,?)",
            (json.dumps({"text": "logo", "attachments": [
                {"type": "image/png", "name": "image.png", "data": png}]}),
             2_000_000_000_000))
        # A non-image attachment must not be offered as a logo.
        con.execute(
            "INSERT INTO transcript_entries VALUES (3,'agent:main:webchat:new','user',?,?)",
            (json.dumps({"text": "doc", "attachments": [
                {"type": "application/pdf", "name": "a.pdf", "data": "eA=="}]}),
             2_100_000_000_000))
        con.commit()
        con.close()

        chat = find_chat_images(db_path=db)
        check("transcript lookup finds both images", len(chat), 2)
        check("newest message wins, not newest file", chat[0]["session_key"],
              "agent:main:webchat:new")
        check("inline source is labelled", chat[0]["source"], "inline")
        check("staged source is labelled", chat[1]["source"], "staged")
        check("non-image attachments are excluded",
              all(c["mime"].startswith("image/") for c in chat), True)
        check("carries the message timestamp", chat[0]["sent_at"], 2_000_000_000.0)
        check("filters by session",
              len(find_chat_images(db_path=db, session="old")), 1)
        check("missing database returns empty, does not raise",
              find_chat_images(db_path=root / "nope.db"), [])

        # Extraction must reproduce the exact bytes the user sent.
        out = materialize_chat_image(chat[0], dest_dir=root / "out", db_path=db)
        check("extracted file exists", out.is_file(), True)
        check("extracted bytes round-trip", out.read_bytes(), base64.b64decode(png))
        check("extension comes from the attachment", out.suffix, ".png")
        check("extraction is idempotent",
              materialize_chat_image(chat[0], dest_dir=root / "out", db_path=db), out)
        raises("a staged ref with no blob on disk fails loudly",
               lambda: materialize_chat_image(chat[1], dest_dir=root / "out",
                                              db_path=db, media_root=root / "empty"),
               contains="not under")

        found = find_attachment_images(media_root=root)
        check("finds both staged images, skips the text blob", len(found), 2)
        check("reports a usable absolute path",
              all(Path(f["path"]).is_file() for f in found), True)
        check("reports the session", {f["session"] for f in found}, {"sess-1"})
        check("filters by session",
              len(find_attachment_images(media_root=root, session="sess-1")), 2)
        check("unknown session yields nothing",
              find_attachment_images(media_root=root, session="nope"), [])
        check("honours the limit",
              len(find_attachment_images(media_root=root, limit=1)), 1)
        # The empty case is the *common* one — a small webchat image is never
        # written to disk — so it must return cleanly, not raise.
        check("absent media root returns empty, does not raise",
              find_attachment_images(media_root=Path("/nonexistent/xyz")), [])

        body, content_type = _multipart({"f": "v"}, "l.png", "image/png", b"data")
        boundary = content_type.split("boundary=")[1]
        check("multipart has 3 boundary occurrences", body.count(boundary.encode()), 3)
        check("multipart terminates correctly", body.endswith(b"--\r\n"), True)
        check("multipart carries the payload", b"data" in body, True)
    finally:
        if saved is not None:
            os.environ["PINATA_JWT"] = saved


def tier7_import_graph() -> None:
    """The read path must not be able to sign. Structure, not convention."""
    section("Tier 7 — capability separation")
    source_dir = Path(__file__).resolve().parent

    read_src = (source_dir / "pools_read.py").read_text(encoding="utf-8")
    # Match import statements, not the substring: the module's own docstring
    # mentions secp256k1 to explain why it is absent, and a naive `in` check
    # fails on the documentation rather than on a real import.
    import re

    imports = re.findall(r"^\s*(?:from|import)\s+\S+.*$", read_src, re.MULTILINE)
    check("pools_read does not import secp256k1",
          any("secp256k1" in line for line in imports), False)
    check("pools_read does not import tx",
          any(re.search(r"poolsfun\.tx\b", line) for line in imports), False)
    check("pools_read never calls resolve_private_key",
          "resolve_private_key(" in read_src, False)

    # Import it for real and assert no signing function is reachable.
    import importlib

    module = importlib.import_module("pools_read")
    reachable = set()
    for value in vars(module).values():
        mod = getattr(value, "__module__", "")
        if isinstance(mod, str) and mod.startswith("poolsfun"):
            reachable.add(mod)
    check("no signing module in pools_read's namespace",
          any("secp256k1" in m for m in reachable), False)
    check("sys.modules has no secp256k1 after importing pools_read",
          any("secp256k1" in m for m in sys.modules), False)

    plan_src = (source_dir / "poolsfun" / "plan.py").read_text(encoding="utf-8")
    check("plan.py does not import secp256k1", "secp256k1" in plan_src, False)

    from poolsfun import account
    check("account.py exposes no sign_digest", hasattr(account, "sign_digest"), False)
    from poolsfun import secp256k1
    check("secp256k1.py does expose sign_digest",
          hasattr(secp256k1, "sign_digest"), True)

    write_src = (source_dir / "pools_write.py").read_text(encoding="utf-8")
    # One definition plus one call site per write path (launch, the shared locker
    # writer, set-fee-recipient, approve). A new write command that forgets the
    # gate moves this count.
    call_sites = write_src.count("_confirmed(") - write_src.count("def _confirmed(")
    check("every write path goes through the confirm gate", call_sites, 4)
    check("_send refuses planning mode",
          "planning mode; it cannot broadcast" in write_src, True)


def tier8_cli_contract() -> None:
    section("Tier 8 — CLI surface")
    import importlib

    from poolsfun.fmt import parse_args

    args = parse_args(["launch", "--name", "My Token", "--symbol=MYT",
                       "--broadcast", "--dev-buy", "0.001"])
    check("positional command", args["_"], ["launch"])
    check("--flag value", args["name"], "My Token")
    check("--flag=value", args["symbol"], "MYT")
    check("bare flag is True", args["broadcast"], True)
    check("hyphenated keys are preserved", args["dev-buy"], "0.001")

    read_mod = importlib.import_module("pools_read")
    write_mod = importlib.import_module("pools_write")
    check("read commands", sorted(read_mod.COMMANDS),
          ["assets", "fees", "find-image", "mine-salt", "preflight", "simulate",
           "token"])
    check("write commands", sorted(write_mod.COMMANDS),
          ["approve", "claim", "collect", "collect-and-claim", "launch",
           "set-fee-recipient"])
    # Every write command must be reachable by the name it prints in its own
    # copy-paste hint, or the hint is a dead end.
    for name in write_mod.COMMANDS:
        check(f"{name} is dispatchable", callable(write_mod.COMMANDS[name]), True)

    check("read usage mentions every command",
          all(c in read_mod.USAGE for c in read_mod.COMMANDS), True)
    check("write usage mentions every command",
          all(c in write_mod.USAGE for c in write_mod.COMMANDS), True)
    check("usage states the price is fixed",
          "cannot be set" in write_mod.USAGE, True)


def tier9_chain_constants() -> None:
    section("Tier 9 — pinned deployment constants")
    from poolsfun import chains

    check("chain id", chains.CHAIN_ID, 4663)
    check("rpc is hardcoded", chains.RPC_URL,
          "https://rpc.mainnet.chain.robinhood.com/")
    check("factory", chains.PARTY_FACTORY,
          "0x626C3d09B65bF5d1D40E0D5F25e19fa49783B3D4")
    check("locker", chains.PARTY_LOCKER,
          "0x35E41f84d3fD61d4648F0c8B41a1E7d301bCd75E")
    check("weth", chains.WETH, "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73")
    check("usdg", chains.USDG, "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")
    check("signer env", chains.ENV_SIGNER, "POOLSFUN_PRIVATE_KEY")
    check("no RPC env var is declared", hasattr(chains, "ENV_RPC"), False)

    check("paired default is WETH", chains.resolve_paired_asset(None), chains.WETH)
    check("--paired weth", chains.resolve_paired_asset("weth"), chains.WETH)
    check("--paired USDG is case-insensitive",
          chains.resolve_paired_asset("USDG"), chains.USDG)
    check("--paired accepts an address",
          chains.resolve_paired_asset(chains.WETH.lower()), chains.WETH)
    raises("--paired rejects nonsense",
           lambda: chains.resolve_paired_asset("doge"), contains="unknown paired asset")
    check("asset labels", chains.asset_label(chains.WETH), "WETH")

    saved = os.environ.pop("POOLSFUN_PRIVATE_KEY", None)
    try:
        chains._env_loaded = True
        raises("missing key names the variable and the fix",
               chains.resolve_private_key, contains="POOLSFUN_PRIVATE_KEY")
    finally:
        if saved is not None:
            os.environ["POOLSFUN_PRIVATE_KEY"] = saved

    # A key is never accepted from the command line.
    write_src = (Path(__file__).resolve().parent / "pools_write.py").read_text(encoding="utf-8")
    check("no --private-key flag exists", "private-key" in write_src, False)


def tier10_signing_roundtrip() -> None:
    """Signing correctness, with a well-known throwaway key. Nothing is sent."""
    section("Tier 10 — signing")
    from poolsfun.account import account_from_private_key
    from poolsfun.secp256k1 import sign_digest
    from poolsfun.tx import serialize_transaction, sign_transaction

    # Hardhat account #1. Public, funded nowhere, safe to pin.
    key = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    account = account_from_private_key(key)
    check("derives the known address", account["address"],
          "0x70997970C51812dc3A010C7d01b50e0d17dc79C8")
    check("account dict never carries the key", "privateKey" in account, False)

    check("RFC 6979 is deterministic",
          sign_digest(b"\x01" * 32, key), sign_digest(b"\x01" * 32, key))

    tx = {"type": "eip1559", "chainId": 4663, "nonce": 0,
          "maxPriorityFeePerGas": 10**9, "maxFeePerGas": 2 * 10**9,
          "to": "0x626C3d09B65bF5d1D40E0D5F25e19fa49783B3D4",
          "value": 10**15, "data": "0x", "gas": 7_000_000}
    signed = sign_transaction(dict(tx), key)
    check("recovers to the signer", signed["from"], account["address"])
    check("serialises as type 2", signed["raw"].startswith("0x02"), True)
    check("signing is deterministic", sign_transaction(dict(tx), key)["raw"],
          signed["raw"])
    check("a different chain id yields different bytes",
          sign_transaction({**tx, "chainId": 8453}, key)["raw"] != signed["raw"], True)
    check("unsigned serialisation differs from signed",
          serialize_transaction(tx) != signed["raw"], True)


def tier11_hostile_input() -> None:
    """Argument handling under abuse. Every defect here was found by review."""
    section("Tier 11 — hostile and malformed input")
    import shlex

    from poolsfun.chains import resolve_paired_asset, resolve_private_key
    from poolsfun.fmt import opt_float, opt_int, opt_str, parse_args
    from poolsfun.plan import require_launchable

    # A key with one bad character must never reach an exception message.
    bad = "0x" + "5" * 63 + "Z"
    os.environ["POOLSFUN_PRIVATE_KEY"] = bad
    try:
        import poolsfun.chains as chains
        chains._env_loaded = True
        try:
            resolve_private_key()
            FAILURES.append("malformed key: expected a raise")
        except RuntimeError as exc:
            global CHECKS
            CHECKS += 1
            if bad[2:] in str(exc) or bad[2:20] in str(exc):
                FAILURES.append("malformed key: KEY MATERIAL LEAKED into the error")
        raises("malformed key names the variable",
               resolve_private_key, contains="POOLSFUN_PRIVATE_KEY")
    finally:
        os.environ.pop("POOLSFUN_PRIVATE_KEY", None)

    # Bare value-taking flags must be rejected, never coerced to 1/True.
    for flag in ("slippage-bps", "deadline-secs", "max-salt-attempts"):
        raises(f"bare --{flag} is rejected",
               lambda f=flag: opt_int({f: True}, f, 100), contains="needs a value")
    raises("bare --image is rejected",
           lambda: opt_str({"image": True}, "image"), contains="needs a value")
    raises("bare --gas-multiplier is rejected",
           lambda: opt_float({"gas-multiplier": True}, "gas-multiplier", 1.3),
           contains="needs a value")
    raises("bare --paired is rejected",
           lambda: resolve_paired_asset(True), contains="needs a value")

    check("absent flags fall back to the default", opt_int({}, "x", 42), 42)
    check("opt_str returns None when absent", opt_str({}, "x"), None)
    check("opt_str trims", opt_str({"x": "  v  "}, "x"), "v")

    # Slippage bounds: a negative value would set devBuyMinOut above the exact
    # simulated fill, guaranteeing an on-chain DevBuyTooLittle() revert.
    raises("negative slippage is rejected",
           lambda: opt_int({"slippage-bps": "-500"}, "slippage-bps", 100,
                           minimum=0, maximum=9_999), contains="at least 0")
    raises("slippage >= 100% is rejected",
           lambda: opt_int({"slippage-bps": "20000"}, "slippage-bps", 100,
                           minimum=0, maximum=9_999), contains="at most 9999")
    raises("non-numeric slippage is rejected",
           lambda: opt_int({"slippage-bps": "lots"}, "slippage-bps", 100),
           contains="whole number")

    # The paused switch must fail closed when it could not be read.
    raises("unknown paused state refuses to launch",
           lambda: require_launchable({"paused": None, "locker": "0x" + "11" * 20}),
           contains="unknown state")
    raises("paused factory refuses to launch",
           lambda: require_launchable({"paused": True, "locker": "0x" + "11" * 20}),
           contains="paused")
    raises("missing locker refuses to launch",
           lambda: require_launchable({"paused": False, "locker": "0x" + "00" * 20}),
           contains="locker")
    require_launchable({"paused": False, "locker": "0x" + "11" * 20})

    # The replay hint is a line the user is told to run: it must be shell-safe.
    import pools_write

    hostile = 'MyToken$(touch /tmp/pwned)`id`;rm -rf /'
    line = pools_write._replay_command({"_": ["launch"], "name": hostile, "symbol": "T"})
    check("replay quotes command substitution", "$(touch" in line and "'" in line, True)
    check("replay round-trips through the shell lexer",
          shlex.split(line)[shlex.split(line).index("--name") + 1], hostile)
    check("replay leaves no bare metacharacter",
          any(tok == hostile for tok in shlex.split(line)), True)

    # parse_args' own contract, which everything above depends on.
    args = parse_args(["launch", "--image", "--name", "T"])
    check("bare flag before another flag yields True", args["image"], True)
    check("the following flag is not consumed as a value", args["name"], "T")


def main() -> int:
    print("poolsdotfun-token-launcher selftest — offline, no network")
    for tier in (tier1_reference_calldata, tier2_create2, tier3_error_selectors,
                 tier4_curve_math, tier5_plan_hash, tier6_metadata,
                 tier7_import_graph, tier8_cli_contract, tier9_chain_constants,
                 tier10_signing_roundtrip, tier11_hostile_input):
        try:
            tier()
            print("   ok")
        except BaseException as exc:  # noqa: BLE001 — report, keep going
            FAILURES.append(f"{tier.__name__} raised: {exc!r}")
            print(f"   ERROR {exc!r}")

    print(f"\n{CHECKS} checks, {len(FAILURES)} failures")
    for failure in FAILURES:
        print(f"  FAIL  {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
