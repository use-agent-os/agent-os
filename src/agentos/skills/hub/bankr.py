"""Bankr skill source — browses and installs skills from BankrBot/skills.

The Bankr repository (https://github.com/BankrBot/skills) publishes each skill
as a directory containing ``SKILL.md`` + ``catalog.json``. This source reads the
catalog live from GitHub (cached in-memory for a short TTL) so users can browse
and install with one click. Downloading and installation are delegated to
:class:`GitHubSource`, which already fetches the whole skill directory and reads
the ``SKILL.md`` frontmatter; the security scan, quarantine, and lockfile
handling live in :class:`SkillInstaller` and are reused unchanged.

Rather than crawling the whole repository tree (~100 skills — two HTTP requests
each, which trips GitHub's rate limit), this source loads only the fixed
allowlist in ``_ALLOWED_SLUGS``: the slugs are known ahead of time, so it fetches
their ``catalog.json`` + ``SKILL.md`` directly and never calls the git-tree API.

Only skills whose ``catalog.json`` declares ``install.type == "bankr"`` (i.e.
they live in the repo and install directly) are listed. ``external`` skills —
whose install runs a third-party command — are skipped.

Bankr also hosts skills that never land in that repository: anyone can publish
one from bankr.bot, where it lives under the author's wallet address and is
served as JSON by ``api.bankr.bot/public/skills/<wallet>/<slug>`` — body and all.
Those are carried by a second allowlist, ``_ALLOWED_USER_SKILLS``, and take a
separate load/fetch path: there is no repository to clone and no
``catalog.json``, so the SKILL.md is synthesized from the API payload instead of
being downloaded through :class:`GitHubSource`. That allowlist is empty at the
moment — the one skill it carried, ``stock-premium-lp-manager``, was superseded
by ``aero-stock-lp`` in the repository — but the path is live and is what a
future bankr.bot-published skill is added to.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import structlog

from agentos.env import trust_env as _trust_env
from agentos.skills.hub.github import GitHubSource, _frontmatter_field, _parse_identifier
from agentos.skills.hub.source import SkillBundle, SkillMeta, SkillSource, infer_category

log = structlog.get_logger(__name__)

_DEFAULT_REPO = "BankrBot/skills"
_DEFAULT_REF = "main"
# Only these skills are loaded from BankrBot/skills. Fetching the whole repo tree
# and every skill's catalog.json + SKILL.md (~100 skills) trips GitHub's rate
# limit (429); since the slugs are fixed we fetch just these directly.
_ALLOWED_SLUGS: tuple[str, ...] = ("bankr", "bankr-token-scam-analysis", "aero-stock-lp")
# Bankr-hosted skills published from bankr.bot rather than into the repository,
# as ``<wallet>/<slug>``. Same reasoning as the repo allowlist — the registry has
# no public index, so the entries are named here rather than crawled. Empty for
# now: ``stock-premium-lp-manager`` was retired in favour of the repo-published
# ``aero-stock-lp`` above, which covers the same tokenized-equity LP workflow.
_ALLOWED_USER_SKILLS: tuple[str, ...] = ()
# How to install a command a repository skill requires but does not say how to
# get. ``bankr`` declares ``requires.bins: [bankr]`` and names the package only in
# prose, so it installs fine and then sits at "needs setup" with no way forward.
# Keyed by slug and written under ``metadata.agentos``, which the loader overlays
# on the skill's own namespace. An ``install`` the skill declares itself wins.
_INSTALL_SPECS: dict[str, tuple[dict[str, Any], ...]] = {
    "bankr": (
        {
            "id": "bankr-cli",
            "kind": "npm",
            "label": "Install Bankr CLI (npm)",
            "package": "@bankr/cli",
            "bins": ["bankr"],
        },
    ),
}
_USER_API_BASE = "https://api.bankr.bot/public/skills"
_USER_PAGE_BASE = "https://bankr.bot/skills"
_USER_HOSTS = {"bankr.bot", "www.bankr.bot"}
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Mirrors the installer's safe-name rule, so an allowlisted slug can always be
# used as the installed directory name.
_USER_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# The registry body is community-controlled; cap what we will turn into a
# SKILL.md so a hostile payload cannot blow up a browse response or an install.
_MAX_USER_CONTENT_CHARS = 256 * 1024
_MAX_USER_DESCRIPTION_CHARS = 2000
_MAX_USER_TAGS = 20
# Brand avatar for Bankr cards. The catalogs ship ``logo: null`` and store their
# emoji in SKILL.md frontmatter (in formats the browse layer can't reliably
# parse), so browse cards fall back to this shared brand mark instead of a bare
# initials box.
_BANKR_EMOJI = "📺"
_CATALOG_TTL_SECONDS = 15 * 60
# After a failed catalog fetch, don't retry for this long — the router fans
# every search out to all sources, so an un-throttled retry would add the full
# HTTP timeout to every search for the duration of a GitHub outage.
_FAILURE_RETRY_SECONDS = 60
_CATALOG_CONCURRENCY = 16


@dataclass(frozen=True)
class _UserSkillRef:
    """A skill published on bankr.bot, addressed by author wallet + slug."""

    wallet: str
    slug: str

    @property
    def key(self) -> str:
        return f"{self.wallet.lower()}/{self.slug.lower()}"

    @property
    def api_url(self) -> str:
        return f"{_USER_API_BASE}/{self.wallet}/{self.slug}"

    @property
    def page_url(self) -> str:
        return f"{_USER_PAGE_BASE}/{self.wallet}/{self.slug}"


def _parse_user_identifier(identifier: str) -> _UserSkillRef | None:
    """Parse a bankr.bot skill URL (or a bare ``<wallet>/<slug>``).

    Returns ``None`` for anything that is not shaped like a Bankr user skill,
    including a wallet that is not a 40-hex address or a slug that could not be
    used as a directory name — those are rejected here rather than at install
    time, so a malformed identifier never reaches the network.
    """
    raw = identifier.strip()
    if raw.startswith("bankr.bot/") or raw.startswith("www.bankr.bot/"):
        raw = "https://" + raw

    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if parsed.netloc.lower() not in _USER_HOSTS:
            return None
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) != 3 or parts[0] != "skills":
            return None
        wallet, slug = parts[1], parts[2]
    else:
        parts = raw.split("/")
        if len(parts) != 2:
            return None
        wallet, slug = parts

    if not _WALLET_RE.match(wallet) or not _USER_SLUG_RE.match(slug):
        return None
    return _UserSkillRef(wallet=wallet, slug=slug)


def _user_skill_markdown(name: str, description: str, tags: Sequence[str], content: str) -> str:
    """Return SKILL.md text for a registry payload that ships only a body.

    The public API keeps name/description/tags beside the markdown, but every
    consumer downstream (installer, loader, scanner) expects one file with
    frontmatter — so synthesize it. A body that already carries its own block is
    returned unchanged rather than nested inside a second one.
    """
    import yaml

    body = content.lstrip("\n")
    if body.startswith("---") and body.find("\n---", 3) != -1:
        return body

    front = yaml.safe_dump(
        {"name": name, "description": description, "tags": list(tags)},
        sort_keys=False,
        allow_unicode=True,
        width=4096,
    )
    return f"---\n{front}---\n\n{body}"


def _with_install_specs(skill_md: str, specs: Sequence[dict[str, Any]]) -> str:
    """Return ``skill_md`` with ``specs`` added to its frontmatter's install list.

    Returns the input unchanged when the frontmatter cannot be parsed or already
    declares an install: a skill that installs with a manual setup step beats one
    that fails to install, and the skill's own answer is better than ours.
    """
    import yaml

    from agentos.skills.loader import parse_frontmatter_yaml

    if not skill_md.startswith("---"):
        return skill_md
    end = skill_md.find("\n---", 3)
    body_start = skill_md.find("\n", end + 1) if end != -1 else -1
    if body_start == -1:
        return skill_md
    frontmatter = parse_frontmatter_yaml(skill_md[4:end])
    if frontmatter is None:
        return skill_md
    metadata = frontmatter.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return skill_md

    namespaces = [metadata, *(value for value in metadata.values() if isinstance(value, dict))]
    if any(namespace.get("install") for namespace in namespaces):
        return skill_md
    agentos = metadata.setdefault("agentos", {})
    if not isinstance(agentos, dict):
        return skill_md
    agentos["install"] = [dict(spec) for spec in specs]

    rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=4096)
    return f"---\n{rendered}---\n{skill_md[body_start + 1 :]}"


def _matches(meta: SkillMeta, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    # ``author`` is in the haystack because a registry skill's handle is often
    # the only name a user remembers it by, and it stopped being searchable the
    # moment it moved out of ``provider``.
    haystack = " ".join(
        [meta.name, meta.provider, meta.author, meta.category, meta.description, *meta.tags]
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
        allowlist: Sequence[str] = _ALLOWED_SLUGS,
        user_allowlist: Sequence[str] = _ALLOWED_USER_SKILLS,
    ) -> None:
        self._github = GitHubSource(token=token)
        self._repo = repo
        self._ref = ref
        self._allowlist = tuple(allowlist)
        # Malformed entries are dropped at construction rather than surviving as
        # strings that can never match — an allowlist that silently accepts a
        # typo is worse than one that is short.
        self._user_refs = tuple(
            ref_ for ref_ in (_parse_user_identifier(entry) for entry in user_allowlist) if ref_
        )
        self._user_keys = frozenset(ref_.key for ref_ in self._user_refs)
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

    def _is_allowlisted(self, identifier: str) -> bool:
        """Return True when ``identifier`` names an allowlisted skill in this repo.

        ``inspect``/``fetch`` delegate to :class:`GitHubSource`, which will
        download any repository it is pointed at. The delegation has to be
        gated: a hub install records its publisher from the *source* it came
        through, so an unchecked delegate lets
        ``skills.install(identifier="…/attacker/skill", source="bankr")``
        install arbitrary code and have it render under Bankr's name. The
        allowlist is the boundary this source already declares — enforce it on
        the install path too, not only when listing the catalog.
        """
        ref = _parse_identifier(identifier)
        if ref is None:
            return False
        if ref.repo_full.lower() != self._repo.lower():
            return False
        return ref.skill_dir.strip("/") in self._allowlist

    def _allowlisted_user_ref(self, identifier: str) -> _UserSkillRef | None:
        """Return the user-skill this identifier names, when it is allowlisted.

        Same boundary as :meth:`_is_allowlisted`, for the registry half of the
        source: bankr.bot serves every published skill from one host, so without
        this gate ``skills.install(source="bankr")`` could pull any author's
        skill and record it as having come through Bankr's hub.
        """
        ref = _parse_user_identifier(identifier)
        if ref is None or ref.key not in self._user_keys:
            return None
        return ref

    async def inspect(self, identifier: str) -> SkillMeta | None:
        user_ref = self._allowlisted_user_ref(identifier)
        if user_ref is not None:
            loaded = await self._load_user_skill(user_ref)
            return loaded[0] if loaded else None
        if not self._is_allowlisted(identifier):
            log.warning("bankr.identifier_rejected", op="inspect")
            return None
        return await self._github.inspect(identifier)

    async def fetch(self, identifier: str) -> SkillBundle | None:
        user_ref = self._allowlisted_user_ref(identifier)
        if user_ref is not None:
            loaded = await self._load_user_skill(user_ref)
            if loaded is None:
                return None
            meta, skill_md = loaded
            return SkillBundle(name=user_ref.slug, files={"SKILL.md": skill_md}, meta=meta)
        if not self._is_allowlisted(identifier):
            log.warning("bankr.identifier_rejected", op="fetch")
            return None
        bundle = await self._github.fetch(identifier)
        ref = _parse_identifier(identifier)
        specs = _INSTALL_SPECS.get(ref.skill_dir.strip("/")) if ref else None
        if bundle is None or not specs:
            return bundle
        fetched_md = bundle.skill_md
        if fetched_md:
            bundle.files["SKILL.md"] = _with_install_specs(fetched_md, specs)
        return bundle

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
        """Fetch catalog.json + SKILL.md for each allowlisted slug directly.

        No git-tree crawl: the slugs are fixed, so this issues at most two HTTP
        requests per skill. Returns ``None`` on total failure (so the negative
        cache retries after a short delay) — including when every entry errors
        (e.g. a 429 burst), which would otherwise cache an empty list for the
        full TTL.
        """
        import httpx

        if not self._allowlist and not self._user_refs:
            return []

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                sem = asyncio.Semaphore(_CATALOG_CONCURRENCY)

                async def _load_one(slug: str) -> SkillMeta | None:
                    async with sem:
                        return await self._load_catalog_entry(client, slug)

                async def _load_user(ref: _UserSkillRef) -> SkillMeta | None:
                    async with sem:
                        loaded = await self._load_user_entry(client, ref)
                        return loaded[0] if loaded else None

                loaded = await asyncio.gather(
                    *(_load_one(s) for s in self._allowlist),
                    *(_load_user(r) for r in self._user_refs),
                )
        except Exception as exc:
            log.warning("bankr.fetch_failed", error=str(exc))
            return None

        metas = [m for m in loaded if m is not None]
        if not metas:
            # Every allowlisted skill failed to load (outage / rate limit) —
            # treat as a fetch failure so we retry rather than cache empty.
            return None
        metas.sort(key=lambda m: m.name)
        return metas

    async def _load_catalog_entry(self, client, slug: str) -> SkillMeta | None:
        """Fetch and parse one skill's catalog.json, then SKILL.md. Skips on
        catalog error.

        Only *installable* skills get the second SKILL.md fetch — external
        installs and malformed catalogs are discarded first, so we never spend
        a request on a skill that won't be listed. The description is
        load-bearing for browse search (it feeds the ``_matches`` haystack), so
        it is fetched eagerly here, not lazily per card; a failed description
        fetch degrades to an empty string rather than dropping the skill.
        Results are cached for the catalog TTL, so browsing stays a bounded
        burst per refresh.
        """
        headers = self._github._headers()
        catalog_url = f"{self._raw_base}/{slug}/catalog.json"
        try:
            resp = await client.get(catalog_url, headers=headers)
            resp.raise_for_status()
            catalog = json.loads(resp.content)
        except Exception as exc:
            log.warning("bankr.catalog_failed", slug=slug, error=str(exc))
            return None
        if not isinstance(catalog, dict):
            return None
        meta = self._meta_from_catalog(slug, catalog)
        if meta is None:
            return None
        skill_md_url = f"{self._raw_base}/{slug}/SKILL.md"
        meta.description = await self._load_description(client, slug, skill_md_url, headers)
        return meta

    async def _load_user_skill(self, ref: _UserSkillRef) -> tuple[SkillMeta, str] | None:
        """Load one bankr.bot skill on its own client (inspect/fetch entry point)."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                return await self._load_user_entry(client, ref)
        except Exception as exc:
            log.warning("bankr.user_fetch_failed", slug=ref.slug, error=str(exc))
            return None

    async def _load_user_entry(self, client, ref: _UserSkillRef) -> tuple[SkillMeta, str] | None:
        """Fetch one registry payload and return its (meta, SKILL.md) pair.

        Deliberately unauthenticated: the public endpoint needs no credentials,
        and the GitHub token this source carries for the repo half has no
        business being sent to a different host.
        """
        try:
            resp = await client.get(ref.api_url)
            resp.raise_for_status()
            payload = json.loads(resp.content)
        except Exception as exc:
            log.warning("bankr.user_skill_failed", slug=ref.slug, error=str(exc))
            return None
        return self._user_document(ref, payload)

    def _user_document(self, ref: _UserSkillRef, payload: object) -> tuple[SkillMeta, str] | None:
        """Build (meta, SKILL.md) from a registry payload, or ``None`` if unusable."""
        if not isinstance(payload, dict) or payload.get("success") is False:
            return None
        skill = payload.get("skill")
        if not isinstance(skill, dict):
            return None

        content = skill.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        if len(content) > _MAX_USER_CONTENT_CHARS:
            log.warning("bankr.user_skill_oversized", slug=ref.slug, chars=len(content))
            return None

        description = str(skill.get("description") or "")[:_MAX_USER_DESCRIPTION_CHARS]
        raw_tags = skill.get("tags")
        tags = (
            [str(tag)[:64] for tag in raw_tags[:_MAX_USER_TAGS]]
            if isinstance(raw_tags, list)
            else []
        )
        raw_author = skill.get("author")
        author = raw_author if isinstance(raw_author, dict) else {}
        # Bankr's brand, the wallet's credit. A wallet-published skill reaches
        # this function only because its ``<wallet>/<slug>`` is named in
        # ``_ALLOWED_USER_SKILLS`` — a decision made in this repository and
        # shipped in the wheel, the same review path a partner catalog entry
        # gets — so it is Bankr-distributed and groups with Bankr. The brand is
        # the allowlist's, never the payload's: nothing the registry returns can
        # change ``provider``, so a hostile row cannot mint one for itself (see
        # ``agentos.skills.publishers``). The handle rides along as ``author``,
        # an attribution string the UI must render as credit and not identity.
        provider = "Bankr"
        author_credit = str(author.get("handle") or author.get("displayName") or "")

        meta = SkillMeta(
            name=ref.slug,
            author=author_credit,
            description=description,
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=ref.page_url,
            homepage=ref.page_url,
            provider=provider,
            # Author avatars are served from a third-party CDN the console's CSP
            # does not allow (``img-src 'self' … raw.githubusercontent.com``), so
            # cards use the Bankr brand mark instead of a broken image.
            logo="",
            emoji=_BANKR_EMOJI,
            category=infer_category(ref.slug, provider, tags),
            tags=tags,
        )
        return meta, _user_skill_markdown(ref.slug, description, tags, content)

    async def _load_description(self, client, slug: str, url: str, headers: dict) -> str:
        """Fetch SKILL.md and read its frontmatter description. Empty on error."""
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("bankr.skill_md_failed", slug=slug, error=str(exc))
            return ""
        try:
            text = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        return _frontmatter_field(text, "description")

    def _meta_from_catalog(self, slug: str, catalog: dict) -> SkillMeta | None:
        """Build a browse-time SkillMeta from a parsed catalog.json.

        Returns ``None`` when the skill is not a directly-installable ``bankr``
        skill (e.g. an ``external`` install), so callers can skip it. The
        human-readable description lives in ``SKILL.md`` frontmatter
        (``catalog.json`` has none); ``_load_catalog_entry`` fills it in with a
        follow-up SKILL.md fetch after this meta is built.
        """
        install = catalog.get("install")
        if not isinstance(install, dict) or install.get("type") != "bankr":
            return None

        provider = str(catalog.get("provider") or "")

        setup_raw = catalog.get("setup")
        setup = [str(s) for s in setup_raw] if isinstance(setup_raw, list) else []
        demo_raw = catalog.get("demo")
        demo = demo_raw if isinstance(demo_raw, dict) else {}

        # Prefer a catalog-declared emoji; otherwise fall back to the Bankr
        # brand mark so cards never render a bare initials box.
        emoji = str(catalog.get("emoji") or "") or _BANKR_EMOJI

        return SkillMeta(
            name=slug,
            description="",
            source_id="bankr",
            trust_level="community",
            identifier=self._skill_url(slug),
            homepage=str(catalog.get("providerUrl") or self._skill_url(slug)),
            provider=provider,
            # The catalog's own ``logo`` is deliberately ignored. This is a
            # curated partner catalog: every card in it is Bankr-distributed,
            # so it wears the Bankr brand mark the tab and catalog header wear
            # (``LogoBadge`` falls back to it on an empty logo). Honouring the
            # payload's artwork made ``aero-stock-lp`` — the one entry that
            # ships a ``logo.svg`` — the odd card out, and would let a
            # repository-side edit repaint a partner card's identity.
            logo="",
            emoji=emoji,
            category=infer_category(slug, provider),
            setup=setup,
            demo=demo,
        )
