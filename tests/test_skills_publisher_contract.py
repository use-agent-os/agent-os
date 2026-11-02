"""Publisher is real, allowlisted data — not a string a manifest can claim."""

from __future__ import annotations

import json
from pathlib import Path

from agentos.skills.loader import SkillLoader
from agentos.skills.publishers import RECOGNIZED_PUBLISHERS, resolve_publisher
from agentos.skills.types import SkillPublisher

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"

ROBINHOOD = RECOGNIZED_PUBLISHERS["robinhood"]


def _write_skill(root: Path, name: str, frontmatter: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Synthetic skill.\n{frontmatter}---\n\n# body\n",
        encoding="utf-8",
    )
    return skill_dir


# ── Allowlist ────────────────────────────────────────────────────────────────


def test_recognized_publishers_carry_the_labels_the_ui_shows() -> None:
    assert RECOGNIZED_PUBLISHERS["robinhood"].name == "Robinhood"
    assert RECOGNIZED_PUBLISHERS["bankr"].name == "Bankr"
    for slug, publisher in RECOGNIZED_PUBLISHERS.items():
        assert publisher.id == slug
        assert publisher.url.startswith("https://")


def test_unrecognized_id_resolves_to_no_publisher() -> None:
    assert resolve_publisher({"id": "acme-capital", "name": "Acme"}) == SkillPublisher()


def test_missing_or_malformed_declaration_resolves_to_no_publisher() -> None:
    for raw in (None, {}, [], 7, "", {"id": ""}, {"name": "Robinhood"}):
        assert resolve_publisher(raw) == SkillPublisher()


def test_bare_id_string_selects_the_allowlisted_record() -> None:
    assert resolve_publisher("robinhood") == ROBINHOOD
    assert resolve_publisher(" Robinhood ") == ROBINHOOD


# ── Impersonation guard ──────────────────────────────────────────────────────


def test_declared_brand_fields_are_ignored_in_favour_of_the_allowlist() -> None:
    """A skill selects a publisher; it never gets to describe one."""

    resolved = resolve_publisher(
        {
            "id": "robinhood",
            "name": "Not Robinhood",
            "url": "https://example.com",
            "logo": "https://example.com/logo.png",
        }
    )

    assert resolved == ROBINHOOD
    assert resolved.name != "Not Robinhood"
    assert resolved.url != "https://example.com"
    assert resolved.logo != "https://example.com/logo.png"


def test_third_party_skill_cannot_load_a_partner_brand_it_wrote_itself(tmp_path: Path) -> None:
    """A directory dropped into a writable skills path gets no brand at all."""

    managed = tmp_path / "managed"
    _write_skill(
        managed,
        "totally-legit-trading",
        'publisher:\n  id: robinhood\n  name: "Not Robinhood"\n  url: https://example.com\n',
    )

    loader = SkillLoader(
        bundled_dir=tmp_path / "bundled",
        managed_dir=managed,
        snapshot_path=tmp_path / "snapshot.json",
    )
    skill = loader.get_by_name("totally-legit-trading")

    assert skill is not None
    assert skill.publisher == SkillPublisher()
    assert "example.com" not in (skill.publisher.url + skill.publisher.logo)


def test_only_a_bundled_manifest_may_select_its_own_publisher(tmp_path: Path) -> None:
    """The allowlist stops forged *fields*; the layer stops forged *ids*."""

    bundled = tmp_path / "bundled"
    managed = tmp_path / "managed"
    personal = tmp_path / "personal"
    _write_skill(bundled, "shipped-partner", "publisher:\n  id: robinhood\n")
    _write_skill(managed, "managed-lookalike", "publisher:\n  id: robinhood\n")
    _write_skill(personal, "personal-lookalike", "publisher:\n  id: robinhood\n")

    loader = SkillLoader(
        bundled_dir=bundled,
        managed_dir=managed,
        personal_agents_dir=personal,
        snapshot_path=tmp_path / "snapshot.json",
    )
    skills = {s.name: s for s in loader.load_all()}

    assert skills["shipped-partner"].publisher == ROBINHOOD
    assert skills["managed-lookalike"].publisher == SkillPublisher()
    assert skills["personal-lookalike"].publisher == SkillPublisher()


def test_the_layer_gate_survives_the_snapshot_cache(tmp_path: Path) -> None:
    """The cache stores the layer, so a restored row gets the same answer."""

    bundled = tmp_path / "bundled"
    managed = tmp_path / "managed"
    _write_skill(bundled, "shipped-partner", "publisher:\n  id: robinhood\n")
    _write_skill(managed, "managed-lookalike", "publisher:\n  id: robinhood\n")
    snapshot = tmp_path / "snapshot.json"

    def load() -> dict[str, SkillPublisher]:
        loader = SkillLoader(bundled_dir=bundled, managed_dir=managed, snapshot_path=snapshot)
        return {s.name: s.publisher for s in loader.load_all()}

    load()
    SkillLoader(bundled_dir=bundled, managed_dir=managed, snapshot_path=snapshot).save_snapshot()

    # A snapshot written before the layer gate existed still carries the brand.
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    for entry in data["skills"]:
        entry["publisher"] = {
            "id": "robinhood",
            "name": "Robinhood",
            "url": "https://robinhood.com",
            "logo": "",
        }
    snapshot.write_text(json.dumps(data), encoding="utf-8")

    restored = load()

    assert restored["shipped-partner"] == ROBINHOOD
    assert restored["managed-lookalike"] == SkillPublisher()


def test_a_tampered_snapshot_cannot_inject_a_brand(tmp_path: Path) -> None:
    """The cache file is writable, so it gets the same allowlist check."""

    root = tmp_path / "skills"
    _write_skill(root, "plain", "")
    snapshot = tmp_path / "snapshot.json"
    SkillLoader(bundled_dir=root, snapshot_path=snapshot).save_snapshot()

    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["skills"][0]["publisher"] = {
        "id": "acme-capital",
        "name": "Robinhood",
        "url": "https://example.com",
        "logo": "https://example.com/logo.png",
    }
    snapshot.write_text(json.dumps(data), encoding="utf-8")

    restored = SkillLoader(bundled_dir=root, snapshot_path=snapshot).get_by_name("plain")

    assert restored is not None
    assert restored.publisher == SkillPublisher()


# ── Parsing and snapshot round-trip ──────────────────────────────────────────


def test_publisher_parses_and_survives_snapshot_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "partner-skill", "publisher:\n  id: bankr\n")
    snapshot = tmp_path / "snapshot.json"

    loader = SkillLoader(bundled_dir=root, snapshot_path=snapshot)
    fresh = loader.get_by_name("partner-skill")
    assert fresh is not None
    assert fresh.publisher == RECOGNIZED_PUBLISHERS["bankr"]
    loader.save_snapshot()

    reloaded = SkillLoader(bundled_dir=root, snapshot_path=snapshot)
    assert reloaded.load_snapshot() is not None  # the snapshot really was used
    from_snapshot = reloaded.get_by_name("partner-skill")

    assert from_snapshot is not None
    assert from_snapshot.publisher == RECOGNIZED_PUBLISHERS["bankr"]


def test_a_skill_without_a_publisher_block_still_loads(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "unbranded", "")

    skill = SkillLoader(bundled_dir=root, snapshot_path=tmp_path / "snapshot.json").get_by_name(
        "unbranded"
    )

    assert skill is not None
    assert skill.publisher == SkillPublisher()


def test_a_version_9_snapshot_is_rejected_instead_of_stripping_publisher(tmp_path: Path) -> None:
    """A v9 cache parses cleanly, so only the version bump keeps the brand."""

    root = tmp_path / "skills"
    _write_skill(root, "partner-skill", "publisher:\n  id: robinhood\n")
    snapshot = tmp_path / "snapshot.json"
    SkillLoader(bundled_dir=root, snapshot_path=snapshot).save_snapshot()

    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["version"] = 9
    for entry in data["skills"]:
        entry.pop("publisher", None)
    snapshot.write_text(json.dumps(data), encoding="utf-8")

    stale = SkillLoader(bundled_dir=root, snapshot_path=snapshot)
    assert stale.load_snapshot() is None

    rescanned = stale.get_by_name("partner-skill")
    assert rescanned is not None
    assert rescanned.publisher == ROBINHOOD


# ── Independence from provenance ─────────────────────────────────────────────


def test_publisher_and_provenance_do_not_constrain_each_other(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "brand-only", "publisher:\n  id: robinhood\n")
    _write_skill(
        root,
        "provenance-only",
        "provenance:\n  origin: clawhub-mit0\n  license: MIT-0\n",
    )
    loader = SkillLoader(bundled_dir=root, snapshot_path=tmp_path / "snapshot.json")

    brand_only = loader.get_by_name("brand-only")
    provenance_only = loader.get_by_name("provenance-only")

    assert brand_only is not None and provenance_only is not None
    # A publisher says nothing about where the text came from...
    assert brand_only.publisher == ROBINHOOD
    assert brand_only.provenance.origin == "unknown"
    assert brand_only.provenance.license == "unknown"
    # ...and provenance says nothing about whose name is on the card.
    assert provenance_only.provenance.origin == "clawhub-mit0"
    assert provenance_only.publisher == SkillPublisher()


# ── The bundled partner skills ───────────────────────────────────────────────


def test_bundled_robinhood_skills_declare_the_robinhood_publisher(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    skills = {skill.name: skill for skill in loader.load_all()}

    for name in ("robinhood-agentic-trading", "robinhood-rwa-addresses"):
        assert skills[name].publisher == ROBINHOOD
        # Publisher is additive: the notices pipeline still reads provenance.
        assert skills[name].provenance.origin == "agentos-original"


def test_other_bundled_skills_stay_unbranded(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")

    branded = {skill.name for skill in loader.load_all() if skill.publisher != SkillPublisher()}

    assert branded == {
        "musebook",
        "robinhood-agentic-trading",
        "robinhood-chain-stocks",
        "robinhood-rwa-addresses",
    }
