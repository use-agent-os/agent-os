"""Bankr skill source — browses and installs skills from BankrBot/skills.

The Bankr repository (https://github.com/BankrBot/skills) publishes each skill
as a directory containing ``SKILL.md`` + ``catalog.json``. This source reads the
catalog live from GitHub (cached in-memory for a short TTL) so users can browse
the full catalog and install with one click. Downloading and installation are
delegated to :class:`GitHubSource`, which already fetches the whole skill
directory and reads the ``SKILL.md`` frontmatter; the security scan, quarantine,
and lockfile handling live in :class:`SkillInstaller` and are reused unchanged.

Only skills whose ``catalog.json`` declares ``install.type == "bankr"`` (i.e.
they live in the repo and install directly) are listed. ``external`` skills —
whose install runs a third-party command — are skipped.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import structlog

from agentos.env import trust_env as _trust_env
from agentos.skills.hub.github import GitHubSource
from agentos.skills.hub.source import SkillBundle, SkillMeta, SkillSource

log = structlog.get_logger(__name__)

_DEFAULT_REPO = "BankrBot/skills"
_DEFAULT_REF = "main"
_CATALOG_TTL_SECONDS = 15 * 60
# After a failed catalog fetch, don't retry for this long — the router fans
# every search out to all sources, so an un-throttled retry would add the full
# HTTP timeout to every search for the duration of a GitHub outage.
_FAILURE_RETRY_SECONDS = 60
_CATALOG_CONCURRENCY = 16

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _catalog_slugs(tree: dict) -> list[str]:
    """Return skill slugs that own a top-level ``<slug>/catalog.json`` path."""
    slugs: list[str] = []
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        parts = path.split("/")
        # Only top-level skill directories: exactly "<slug>/catalog.json".
        if len(parts) == 2 and parts[1] == "catalog.json" and parts[0]:
            slugs.append(parts[0])
    return slugs


# Coarse category buckets inferred from the slug/provider so the browse UI can
# offer meaningful filter chips. Keywords match whole slug/provider tokens
# (split on non-alphanumerics), not substrings — so "design" does not become
# "sign" and "alphabet" does not become "bet". Ordered by specificity — first
# keyword hit wins.
_CATEGORY_KEYWORDS: list[tuple[str, frozenset[str]]] = [
    ("trading", frozenset({"trade", "trading", "swap", "uniswap", "dex", "perp", "hyperliquid"})),
    ("defi", frozenset({"defi", "aave", "lend", "yield", "vault", "stake", "liquidity", "token"})),
    ("wallet", frozenset({"wallet", "account", "erc4337", "signer", "sign", "custody"})),
    ("markets", frozenset({"polymarket", "kalshi", "prediction", "bet", "market", "odds"})),
    (
        "social",
        frozenset({"farcaster", "twitter", "neynar", "social", "community", "chat", "message"}),
    ),
    (
        "data",
        frozenset(
            {"alchemy", "zerion", "data", "monitor", "analytics", "index", "scan", "research"}
        ),
    ),
    ("nft", frozenset({"nft", "collectible", "mint", "opensea"})),
    (
        "dev",
        frozenset({"foundry", "contract", "audit", "gas", "deploy", "sdk", "dev", "skill", "eval"}),
    ),
    ("infra", frozenset({"ens", "rpc", "node", "infra", "gateway", "x402", "webhook"})),
]


def _infer_category(slug: str, provider: str) -> str:
    """Return a coarse category for browse filters, or "other" when unknown."""
    tokens = set(_TOKEN_RE.findall(f"{slug} {provider}".lower()))
    for category, keywords in _CATEGORY_KEYWORDS:
        if tokens & keywords:
            return category
    return "other"


def _matches(meta: SkillMeta, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    haystack = " ".join(
        [meta.name, meta.provider, meta.category, meta.description, *meta.tags]
    ).lower()
    return q in haystack


class BankrSource(SkillSource):
    """Skill source backed by the BankrBot/skills GitHub catalog."""

    def __init__(
        self,
        token: str | None = None,
        *,
        repo: str = _DEFAULT_REPO,
        ref: str = _DEFAULT_REF,
    ) -> None:
        self._github = GitHubSource(token=token)
        self._repo = repo
        self._ref = ref
        self._tree_api_url = f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
        self._raw_base = f"https://raw.githubusercontent.com/{repo}/{ref}"
        self._cache_metas: list[SkillMeta] | None = None
        self._cache_at = 0.0
        self._last_failure_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def source_id(self) -> str:
        return "bankr"

    @property
    def trust_level(self) -> str:
        return "community"

    def _skill_url(self, slug: str) -> str:
        return f"https://github.com/{self._repo}/tree/{self._ref}/{slug}"

    async def search(self, query: str, limit: int = 200) -> list[SkillMeta]:
        """List Bankr skills (all when query is empty; filtered otherwise)."""
        metas = await self._load_catalog()
        results = [m for m in metas if _matches(m, query)]
        return results[:limit]

    async def inspect(self, identifier: str) -> SkillMeta | None:
        return await self._github.inspect(identifier)

    async def fetch(self, identifier: str) -> SkillBundle | None:
        return await self._github.fetch(identifier)

    async def _load_catalog(self) -> list[SkillMeta]:
        async with self._lock:
            now = time.monotonic()
            if self._cache_metas is not None and (now - self._cache_at) < _CATALOG_TTL_SECONDS:
                return self._cache_metas
            # Negative cache: after a failed fetch, serve what we have (stale
            # list or empty) without hammering GitHub on every search.
            if (now - self._last_failure_at) < _FAILURE_RETRY_SECONDS:
                return self._cache_metas or []

            metas = await self._fetch_catalog()
            if metas is None:
                self._last_failure_at = time.monotonic()
                return self._cache_metas or []

            self._cache_metas = metas
            self._cache_at = time.monotonic()
            return metas

    async def _fetch_catalog(self) -> list[SkillMeta] | None:
        """Fetch the tree + all catalog.json files. Returns None on tree failure."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                tree_resp = await client.get(self._tree_api_url, headers=self._github._headers())
                tree_resp.raise_for_status()
                tree = tree_resp.json()
                if tree.get("truncated"):
                    log.warning("bankr.tree_truncated")
                    return None

                slugs = _catalog_slugs(tree)
                if not slugs:
                    return []

                sem = asyncio.Semaphore(_CATALOG_CONCURRENCY)

                async def _load_one(slug: str) -> SkillMeta | None:
                    async with sem:
                        return await self._load_catalog_entry(client, slug)

                loaded = await asyncio.gather(*(_load_one(s) for s in slugs))
        except Exception as exc:
            log.warning("bankr.tree_failed", error=str(exc))
            return None

        metas = [m for m in loaded if m is not None]
        metas.sort(key=lambda m: m.name)
        return metas

    async def _load_catalog_entry(self, client, slug: str) -> SkillMeta | None:
        """Fetch and parse one skill's catalog.json. Skips on any error.

        Only catalog.json is fetched (one request per skill) to keep browsing
        fast; the description is filled in later at fetch()/install time.
        """
        url = f"{self._raw_base}/{slug}/catalog.json"
        try:
            resp = await client.get(url, headers=self._github._headers())
            resp.raise_for_status()
            catalog = json.loads(resp.content)
        except Exception as exc:
            log.warning("bankr.catalog_failed", slug=slug, error=str(exc))
            return None
        if not isinstance(catalog, dict):
            return None
        return self._meta_from_catalog(slug, catalog)

    def _meta_from_catalog(self, slug: str, catalog: dict) -> SkillMeta | None:
        """Build a browse-time SkillMeta from a parsed catalog.json.

        Returns ``None`` when the skill is not a directly-installable ``bankr``
        skill (e.g. an ``external`` install), so callers can skip it. The
        human-readable description lives in ``SKILL.md`` frontmatter
        (``catalog.json`` has none); it is filled in at ``fetch()`` time to keep
        browsing fast, so the browse card shows slug + provider + catalog
        demo/setup, but no description.
        """
        install = catalog.get("install")
        if not isinstance(install, dict) or install.get("type") != "bankr":
            return None

        provider = str(catalog.get("provider") or "")
        logo_name = catalog.get("logo")
        logo = (
            f"{self._raw_base}/{slug}/{logo_name}"
            if isinstance(logo_name, str) and logo_name
            else ""
        )

        setup_raw = catalog.get("setup")
        setup = [str(s) for s in setup_raw] if isinstance(setup_raw, list) else []
        demo_raw = catalog.get("demo")
        demo = demo_raw if isinstance(demo_raw, dict) else {}

        return SkillMeta(
            name=slug,
            description="",
            source_id="bankr",
            trust_level="community",
            identifier=self._skill_url(slug),
            homepage=str(catalog.get("providerUrl") or self._skill_url(slug)),
            provider=provider,
            logo=logo,
            category=_infer_category(slug, provider),
            setup=setup,
            demo=demo,
        )
