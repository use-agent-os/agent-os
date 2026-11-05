from __future__ import annotations

import json
from typing import Any

import pytest

from agentos.skills.hub.bankr import _ALLOWED_SLUGS, _ALLOWED_USER_SKILLS, BankrSource

# The fake catalog exercises the filtering paths (installable / external /
# malformed). We hand BankrSource this slug set via ``allowlist=`` so the source
# fetches exactly these directly — no repo tree crawl.
_FIXTURE_SLUGS = ("alchemy", "bankr", "extern", "broken")

# A skill published on bankr.bot rather than into BankrBot/skills: addressed by
# author wallet, served as JSON with the body inline.
_USER_WALLET = "0x" + "ab" * 20
_USER_SLUG = "range-lp-manager"
_USER_IDENT = f"https://bankr.bot/skills/{_USER_WALLET}/{_USER_SLUG}"
_USER_API_PREFIX = f"https://api.bankr.bot/public/skills/{_USER_WALLET}/"


def _catalog(slug: str, *, install_type: str = "bankr", logo: str | None = None) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 1,
            "slug": slug,
            "provider": slug.title(),
            "providerUrl": f"https://{slug}.example",
            "logo": logo,
            "setup": [f"Install {slug}", "Set env var"],
            "demo": {"title": f"{slug}.sh", "language": "bash", "code": f"{slug} run"},
            "install": {"type": install_type, "repoPath": slug},
        }
    ).encode("utf-8")


def _user_payload(
    *,
    content: str = "# Range LP\n\nPlace and rebalance concentrated liquidity.\n",
    **overrides: Any,
) -> bytes:
    skill: dict[str, Any] = {
        "slug": _USER_SLUG,
        "name": _USER_SLUG,
        "description": "Manage range liquidity — premium aware.",
        "tags": ["defi", "liquidity"],
        "content": content,
        "author": {
            "walletAddress": _USER_WALLET,
            "displayName": "Jane Doe",
            "handle": "@janedoe",
        },
    }
    skill.update(overrides)
    return json.dumps({"success": True, "skill": skill}).encode("utf-8")


class _Response:
    def __init__(
        self,
        *,
        json_data: dict[str, Any] | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data or {}
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AsyncClient:
    """Mocks the per-skill BankrBot/skills catalog.json + SKILL.md fetches.

    The source no longer crawls the git tree, so hitting the trees API here is a
    regression — it raises instead.
    """

    catalogs = {
        "alchemy": _catalog("alchemy", logo="alchemy.svg"),
        "bankr": _catalog("bankr", logo=None),
        "extern": _catalog("extern", install_type="external"),
        "broken": b"{ not json",
    }
    skill_mds = {
        "alchemy": b"---\nname: alchemy\ndescription: On-chain data APIs\n---\n# Alchemy\n",
        "bankr": b"---\nname: bankr\ndescription: AI-powered crypto trading agent\n---\n# Bankr\n",
    }
    catalog_calls = 0
    skill_md_calls = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        if "/git/trees/" in url:
            raise AssertionError(f"tree API must not be called: {url}")
        marker = "raw.githubusercontent.com/BankrBot/skills/main/"
        if marker in url:
            slug = url.split(marker, 1)[1].split("/", 1)[0]
            if url.endswith("/SKILL.md"):
                type(self).skill_md_calls += 1
                content = self.skill_mds.get(slug)
                if content is None:
                    return _Response(status_code=404)
                return _Response(content=content)
            type(self).catalog_calls += 1
            return _Response(content=self.catalogs.get(slug, b"{}"))
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture(autouse=True)
def _reset_client_counters() -> None:
    _AsyncClient.catalog_calls = 0
    _AsyncClient.skill_md_calls = 0


def _source() -> BankrSource:
    """Repo-half source: the registry allowlist is exercised separately below."""
    return BankrSource(allowlist=_FIXTURE_SLUGS, user_allowlist=())


@pytest.mark.asyncio
async def test_search_empty_query_lists_all_bankr_skills(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")

    names = {r.name for r in results}
    # bankr + alchemy kept; external skipped; broken JSON skipped.
    assert names == {"alchemy", "bankr"}
    assert all(r.source_id == "bankr" for r in results)
    assert all(r.trust_level == "community" for r in results)


@pytest.mark.asyncio
async def test_search_builds_provider_and_identifier_without_payload_logo(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")
    by_name = {r.name: r for r in results}

    alchemy = by_name["alchemy"]
    assert alchemy.provider == "Alchemy"
    # A catalog-declared logo is ignored: partner-catalog cards wear the Bankr
    # brand mark the tab wears, not whatever artwork the repository ships.
    assert alchemy.logo == ""
    assert alchemy.identifier == "https://github.com/BankrBot/skills/tree/main/alchemy"

    # Same for a null logo; the Bankr brand emoji fills in as the avatar so
    # cards never render a bare initials box.
    assert by_name["bankr"].logo == ""
    assert by_name["bankr"].emoji == "📺"
    assert by_name["alchemy"].emoji == "📺"


@pytest.mark.asyncio
async def test_search_carries_catalog_setup_demo_and_category(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")
    bankr = next(r for r in results if r.name == "bankr")

    assert bankr.setup == ["Install bankr", "Set env var"]
    assert bankr.demo == {"title": "bankr.sh", "language": "bash", "code": "bankr run"}
    # Category is inferred from slug/provider keywords; always non-empty.
    assert bankr.category
    from agentos.skills.hub.source import infer_category

    assert infer_category("uniswap", "Uniswap") == "trading"
    assert infer_category("aeon-defi-monitor", "Aeon") == "defi"
    assert infer_category("zzz-unknown", "Nobody") == "other"


@pytest.mark.asyncio
async def test_search_fills_description_from_skill_md_frontmatter(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("")
    by_name = {r.name: r for r in results}

    assert by_name["bankr"].description == "AI-powered crypto trading agent"
    assert by_name["alchemy"].description == "On-chain data APIs"
    # SKILL.md is fetched only for installable skills — external installs and
    # broken catalogs never trigger a description fetch.
    assert _AsyncClient.skill_md_calls == 2


class _MissingSkillMdClient(_AsyncClient):
    skill_mds: dict[str, bytes] = {}


@pytest.mark.asyncio
async def test_missing_skill_md_keeps_skill_with_empty_description(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _MissingSkillMdClient)

    results = await _source().search("")
    by_name = {r.name: r for r in results}

    # A failed SKILL.md fetch must not drop the skill from the listing.
    assert set(by_name) == {"alchemy", "bankr"}
    assert by_name["bankr"].description == ""


@pytest.mark.asyncio
async def test_external_install_type_is_excluded(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    # "extern" is allowlisted but its catalog declares install.type == external,
    # so it is dropped and never matches a query.
    results = await _source().search("extern")

    assert results == []


@pytest.mark.asyncio
async def test_search_filters_by_query(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    results = await _source().search("alche")

    assert [r.name for r in results] == ["alchemy"]


@pytest.mark.asyncio
async def test_catalog_is_cached_across_searches(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    src = _source()
    await src.search("")
    first_catalog = _AsyncClient.catalog_calls
    # One catalog.json fetch per allowlisted slug — no tree crawl.
    assert first_catalog == len(_FIXTURE_SLUGS)

    await src.search("bankr")

    # Second search hits the cache — no additional network calls.
    assert _AsyncClient.catalog_calls == first_catalog


class _FailingCatalogClient(_AsyncClient):
    async def get(self, url: str, **kwargs: Any) -> _Response:
        if url.endswith("/catalog.json"):
            raise RuntimeError("boom")
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_all_entries_failing_returns_empty_without_raising(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FailingCatalogClient)

    results = await _source().search("")

    assert results == []


class _DefaultAllowlistClient(_AsyncClient):
    """Serves the real default slugs, and any registry skill that is asked for."""

    catalogs = {
        "bankr": _catalog("bankr", logo=None),
        "bankr-token-scam-analysis": _catalog("bankr-token-scam-analysis", logo=None),
        "aero-stock-lp": _catalog("aero-stock-lp", logo="logo.svg"),
    }
    skill_mds = {
        "bankr": b"---\nname: bankr\ndescription: Trading agent\n---\n# Bankr\n",
        "bankr-token-scam-analysis": (
            b"---\nname: scam\ndescription: Scans tokens for scams\n---\n# Scan\n"
        ),
        "aero-stock-lp": (
            b"---\nname: aero-stock-lp\ndescription: LP tokenized stocks onchain\n"
            b"---\n# Aero stock LP\n"
        ),
    }

    async def get(self, url: str, **kwargs: Any) -> _Response:
        if url.startswith("https://api.bankr.bot/public/skills/"):
            slug = url.rsplit("/", 1)[-1]
            return _Response(content=_user_payload(slug=slug, name=slug))
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_default_allowlist_loads_exactly_the_shipped_repo_skills(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _DefaultAllowlistClient)

    # Defaults (no allowlist args) → exactly these, nothing else. The registry
    # half ships empty: ``stock-premium-lp-manager`` was retired in favour of
    # the repo-published ``aero-stock-lp``.
    assert _ALLOWED_SLUGS == ("bankr", "bankr-token-scam-analysis", "aero-stock-lp")
    assert _ALLOWED_USER_SKILLS == ()

    results = await BankrSource().search("")

    assert {r.name for r in results} == {
        "bankr",
        "bankr-token-scam-analysis",
        "aero-stock-lp",
    }
    # Three repo skills → three catalog.json + three SKILL.md fetches, no tree
    # crawl. No registry skill is allowlisted, so the client's api.bankr.bot
    # branch is never reached.
    assert _DefaultAllowlistClient.catalog_calls == 3
    assert _DefaultAllowlistClient.skill_md_calls == 3


@pytest.mark.asyncio
async def test_fetch_and_inspect_delegate_to_github(monkeypatch) -> None:
    calls: dict[str, str] = {}

    async def _fake_fetch(self: Any, identifier: str) -> str:
        calls["fetch"] = identifier
        return "bundle"

    async def _fake_inspect(self: Any, identifier: str) -> str:
        calls["inspect"] = identifier
        return "meta"

    from agentos.skills.hub.github import GitHubSource

    monkeypatch.setattr(GitHubSource, "fetch", _fake_fetch)
    monkeypatch.setattr(GitHubSource, "inspect", _fake_inspect)

    src = BankrSource()
    ident = "https://github.com/BankrBot/skills/tree/main/aero-stock-lp"
    assert await src.fetch(ident) == "bundle"
    assert await src.inspect(ident) == "meta"
    assert calls == {"fetch": ident, "inspect": ident}


_BANKR_IDENT = "https://github.com/BankrBot/skills/tree/main/bankr"
# Upstream's real shape: flow-style metadata with trailing commas, no `install`, and
# a description whose "Chains:" makes the frontmatter invalid as strict YAML.
_BANKR_SKILL_MD = """---
name: bankr
description: Bankr is an AI crypto agent, driven by the `bankr` CLI. Chains: Base, Solana.
metadata:
  {
    "clawdbot":
      {
        "emoji": "📺",
        "homepage": "https://bankr.bot",
        "requires": { "bins": ["bankr"] },
      },
  }
---

# Bankr

---

Install with `npm install -g @bankr/cli`.
"""


async def _fetch_with_skill_md(monkeypatch, skill_md: str, ident: str = _BANKR_IDENT) -> str:
    from agentos.skills.hub.github import GitHubSource
    from agentos.skills.hub.source import SkillBundle

    async def _fake_fetch(self: Any, identifier: str) -> SkillBundle:
        return SkillBundle(name="bankr", files={"SKILL.md": skill_md, "catalog.json": "{}"})

    monkeypatch.setattr(GitHubSource, "fetch", _fake_fetch)
    bundle = await BankrSource().fetch(ident)
    assert bundle is not None
    assert bundle.files["catalog.json"] == "{}"
    return str(bundle.files["SKILL.md"])


@pytest.mark.asyncio
async def test_bankr_skill_installs_with_a_way_to_get_its_cli(monkeypatch, tmp_path) -> None:
    """The Install button comes from `metadata.install`, read by the real loader.

    Upstream requires the `bankr` binary and declares no install, so a catalog
    install ended at "needs setup" with nothing to press.
    """
    from agentos.skills.eligibility import EligibilityContext, diagnose_eligibility
    from agentos.skills.install_kinds import render_install_command
    from agentos.skills.loader import SkillLoader

    skill_md = await _fetch_with_skill_md(monkeypatch, _BANKR_SKILL_MD)

    skill_dir = tmp_path / "managed" / "bankr"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    spec = SkillLoader(managed_dir=tmp_path / "managed").get_by_name("bankr")

    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert spec.metadata.requires.bins == ["bankr"]
    assert spec.metadata.emoji == "📺"
    assert spec.description.endswith("Chains: Base, Solana.")
    assert [s.id for s in spec.metadata.install] == ["bankr-cli"]
    assert render_install_command(spec.metadata.install[0]) == (
        "npm install -g --ignore-scripts @bankr/cli"
    )
    report = diagnose_eligibility(spec, EligibilityContext(has_bin_cache={"bankr": False}))
    assert report.missing_bins == ["bankr"]

    # Upstream's prose is untouched, including a `---` rule in the body.
    assert skill_md.endswith(_BANKR_SKILL_MD[_BANKR_SKILL_MD.index("\n# Bankr") :])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill_md",
    [
        pytest.param("# No frontmatter\n", id="no-frontmatter"),
        pytest.param("---\nname: [unterminated\n---\nbody\n", id="invalid-yaml"),
        pytest.param("---\nname: bankr\nkey: a: b\n---\nbody\n", id="invalid-outside-description"),
        pytest.param("---\nname: bankr\nmetadata: nope\n---\nbody\n", id="scalar-metadata"),
        pytest.param(
            "---\nname: bankr\nmetadata:\n  clawdbot:\n    install:\n      - kind: brew\n"
            "        id: bankr\n---\nbody\n",
            id="declares-its-own-install",
        ),
    ],
)
async def test_bankr_skill_md_is_left_alone_when_it_cannot_or_need_not_change(
    monkeypatch, skill_md: str
) -> None:
    assert await _fetch_with_skill_md(monkeypatch, skill_md) == skill_md


@pytest.mark.asyncio
async def test_install_specs_apply_only_to_the_skill_they_name(monkeypatch) -> None:
    ident = "https://github.com/BankrBot/skills/tree/main/aero-stock-lp"
    assert await _fetch_with_skill_md(monkeypatch, _BANKR_SKILL_MD, ident) == _BANKR_SKILL_MD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identifier",
    [
        # A different repository entirely — the laundering case.
        "https://github.com/attacker/skills/tree/main/bankr",
        # The right repo, but a slug that is not published by Bankr.
        "https://github.com/BankrBot/skills/tree/main/not-a-bankr-skill",
        # The repo root, with no skill directory at all.
        "https://github.com/BankrBot/skills",
        # Not a GitHub reference in the first place.
        "not-an-identifier",
    ],
)
async def test_install_path_refuses_identifiers_outside_the_allowlist(
    monkeypatch, identifier: str
) -> None:
    """A non-Bankr identifier must not be installable *through* the Bankr source.

    A hub install records its publisher from the source it came through, so an
    ungated delegate would let an arbitrary GitHub skill be installed with
    ``source="bankr"`` and then render under Bankr's name and link in the
    Partners group.
    """
    calls: list[str] = []

    async def _fake_fetch(self: Any, ident: str) -> str:
        calls.append(ident)
        return "bundle"

    from agentos.skills.hub.github import GitHubSource

    monkeypatch.setattr(GitHubSource, "fetch", _fake_fetch)
    monkeypatch.setattr(GitHubSource, "inspect", _fake_fetch)

    src = BankrSource()

    assert await src.fetch(identifier) is None
    assert await src.inspect(identifier) is None
    # The delegate is never reached — nothing is downloaded.
    assert calls == []


# ── Registry half: skills published on bankr.bot, not into BankrBot/skills ───


class _UserSkillClient(_AsyncClient):
    """Serves the public registry endpoint; repo URLs fall through to the base."""

    payload = _user_payload()
    user_calls = 0
    last_kwargs: dict[str, Any] = {}

    async def get(self, url: str, **kwargs: Any) -> _Response:
        if url.startswith("https://api.bankr.bot/public/skills/"):
            type(self).user_calls += 1
            type(self).last_kwargs = kwargs
            if url != _USER_API_PREFIX + _USER_SLUG:
                return _Response(status_code=404)
            return _Response(content=self.payload)
        return await super().get(url, **kwargs)


@pytest.fixture(autouse=True)
def _reset_user_counters() -> None:
    _UserSkillClient.user_calls = 0
    _UserSkillClient.last_kwargs = {}
    _UserSkillClient.payload = _user_payload()


def _user_source() -> BankrSource:
    return BankrSource(
        token="github-token",
        allowlist=(),
        user_allowlist=(f"{_USER_WALLET}/{_USER_SLUG}",),
    )


@pytest.mark.asyncio
async def test_registry_skill_is_listed_with_author_tags_and_page_identifier(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)

    results = await _user_source().search("")

    assert len(results) == 1
    meta = results[0]
    assert meta.name == _USER_SLUG
    assert meta.source_id == "bankr"
    assert meta.trust_level == "community"
    assert meta.description == "Manage range liquidity — premium aware."
    assert meta.tags == ["defi", "liquidity"]
    # Bankr's brand, the wallet's credit. The pair is only reachable because it
    # is named in the wheel's ``_ALLOWED_USER_SKILLS``, so it groups with Bankr;
    # the handle stays as a separate attribution field and never as identity.
    assert meta.provider == "Bankr"
    assert meta.author == "@janedoe"
    # No catalog.json to declare a category, so the tags supply one.
    assert meta.category == "defi"
    assert meta.identifier == _USER_IDENT
    assert meta.homepage == _USER_IDENT
    # No avatar URL: the console's CSP would block the author's CDN image.
    assert meta.logo == ""
    assert meta.emoji == "📺"


@pytest.mark.asyncio
async def test_registry_payload_cannot_choose_its_own_brand(monkeypatch) -> None:
    """The brand comes from the allowlist, never from the third-party body.

    ``provider`` decides the lockfile's ``publisher_id`` and therefore which
    partner a card sits under, so a registry row that could set it would be able
    to file itself under Robinhood. The listing is Bankr-branded only because
    the wheel names this wallet/slug pair; the payload gets no say.
    """
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)
    _UserSkillClient.payload = _user_payload(
        provider="Robinhood",
        publisher={"id": "robinhood", "name": "Robinhood"},
        author={"handle": "@robinhood", "walletAddress": _USER_WALLET},
    )

    meta = (await _user_source().search(""))[0]

    assert meta.provider == "Bankr"
    assert meta.author == "@robinhood"


@pytest.mark.asyncio
async def test_registry_skill_is_searchable_by_tag_and_author(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)

    src = _user_source()

    assert [r.name for r in await src.search("liquidity")] == [_USER_SLUG]
    assert [r.name for r in await src.search("janedoe")] == [_USER_SLUG]
    assert await src.search("nothing-matches-this") == []


@pytest.mark.asyncio
async def test_fetch_registry_skill_synthesizes_skill_md_frontmatter(monkeypatch) -> None:
    import httpx
    import yaml

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)

    async def _unreachable(self: Any, identifier: str) -> Any:
        raise AssertionError("registry skills must not be fetched through GitHub")

    from agentos.skills.hub.github import GitHubSource

    monkeypatch.setattr(GitHubSource, "fetch", _unreachable)

    bundle = await _user_source().fetch(_USER_IDENT)

    assert bundle is not None
    assert bundle.name == _USER_SLUG
    assert set(bundle.files) == {"SKILL.md"}
    md = bundle.skill_md
    assert md is not None and md.startswith("---\n")

    front_text, _, body = md[4:].partition("\n---\n")
    front = yaml.safe_load(front_text)
    assert front["name"] == _USER_SLUG
    assert front["description"] == "Manage range liquidity — premium aware."
    assert front["tags"] == ["defi", "liquidity"]
    # The body the registry served is preserved verbatim underneath.
    assert body.strip().startswith("# Range LP")
    assert "concentrated liquidity" in body
    assert bundle.meta is not None and bundle.meta.identifier == _USER_IDENT


@pytest.mark.asyncio
async def test_registry_fetch_does_not_send_the_github_token(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)

    await _user_source().fetch(_USER_IDENT)

    # api.bankr.bot is a different host than the repo half authenticates
    # against — the GitHub token has no business being sent there.
    assert _UserSkillClient.user_calls == 1
    assert not _UserSkillClient.last_kwargs.get("headers")


@pytest.mark.asyncio
async def test_inspect_registry_skill_returns_meta_without_github(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)

    async def _unreachable(self: Any, identifier: str) -> Any:
        raise AssertionError("registry skills must not be inspected through GitHub")

    from agentos.skills.hub.github import GitHubSource

    monkeypatch.setattr(GitHubSource, "inspect", _unreachable)

    meta = await _user_source().inspect(_USER_IDENT)

    assert meta is not None
    assert meta.name == _USER_SLUG
    assert meta.identifier == _USER_IDENT


@pytest.mark.asyncio
async def test_body_that_already_has_frontmatter_is_not_nested(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)
    authored = "---\nname: authored\ndescription: Written by hand\n---\n\n# Authored\n"
    _UserSkillClient.payload = _user_payload(content=authored)

    bundle = await _user_source().fetch(_USER_IDENT)

    assert bundle is not None
    assert bundle.skill_md == authored


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{ not json", id="malformed-json"),
        pytest.param(json.dumps({"success": False}).encode(), id="unsuccessful"),
        pytest.param(json.dumps({"success": True}).encode(), id="no-skill-object"),
        pytest.param(_user_payload(content=""), id="empty-body"),
        pytest.param(_user_payload(content="x" * (256 * 1024 + 1)), id="oversized-body"),
    ],
)
async def test_unusable_registry_payloads_are_dropped(monkeypatch, payload: bytes) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)
    _UserSkillClient.payload = payload

    src = _user_source()

    assert await src.search("") == []
    assert await src.fetch(_USER_IDENT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identifier",
    [
        # Another author's skill on the same host — the laundering case.
        f"https://bankr.bot/skills/{'0x' + 'cd' * 20}/{_USER_SLUG}",
        # The allowlisted author, but a skill they did not get listed for.
        f"https://bankr.bot/skills/{_USER_WALLET}/some-other-skill",
        # A look-alike host.
        f"https://bankr.bot.evil.example/skills/{_USER_WALLET}/{_USER_SLUG}",
        # Path traversal in the slug, and a wallet that is not an address.
        f"https://bankr.bot/skills/{_USER_WALLET}/../../etc/passwd",
        f"https://bankr.bot/skills/not-a-wallet/{_USER_SLUG}",
    ],
)
async def test_registry_install_path_refuses_identifiers_outside_the_allowlist(
    monkeypatch, identifier: str
) -> None:
    """bankr.bot serves every published skill from one host, so the source must
    gate on the exact wallet/slug pair — otherwise any author's skill could be
    installed with ``source="bankr"`` and inherit the hub's provenance."""
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _UserSkillClient)

    src = _user_source()

    assert await src.fetch(identifier) is None
    assert await src.inspect(identifier) is None
    # Rejected before the network, not after.
    assert _UserSkillClient.user_calls == 0


def test_malformed_user_allowlist_entries_are_dropped_at_construction() -> None:
    src = BankrSource(
        allowlist=(),
        user_allowlist=("not-a-wallet/slug", "0xzz/bad", f"{_USER_WALLET}/{_USER_SLUG}"),
    )

    assert [ref.key for ref in src._user_refs] == [f"{_USER_WALLET}/{_USER_SLUG}"]


def test_default_router_exposes_bankr_source(monkeypatch) -> None:
    from agentos.skills.hub import defaults

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    defaults._default_router = None
    try:
        router = defaults.get_default_skill_router()
        assert "bankr" in router.source_ids
    finally:
        defaults._default_router = None
