"""Allowlist of publishers that may carry brand identity on a skill card.

A ``SKILL.md`` is a plain text file that anyone can write and any hub can serve.
If the publisher block in its frontmatter were trusted verbatim, a third-party
skill could ship ``publisher: {id: robinhood, name: Robinhood}`` and inherit a
partner's name, link, and logo in the Skills UI — a phishing surface, not a
metadata feature.

So the frontmatter only *selects* a publisher; it never *describes* one. The
declared id is looked up in :data:`RECOGNIZED_PUBLISHERS`, and the allowlisted
record supplies every displayed field. An id that is not on the list resolves to
an empty publisher, which renders as an ordinary unbranded skill.

The allowlist alone is not enough, because it makes the *ids* forgeable instead
of the fields: any directory an operator drops into a skills path could declare
``publisher: {id: robinhood}`` and sit in the Partners group. So a manifest may
only select a publisher for itself when it ships inside the wheel — see
:func:`resolve_declared_publisher`. Every other skill gets its brand from the
hub catalog row that installed it, recorded in the lockfile
(``LockEntry.publisher_id``), which is why an installed partner skill such as
Bankr still shows its brand while a look-alike on disk does not.
"""

from __future__ import annotations

from agentos.skills.types import SkillLayer, SkillPublisher

#: Publishers allowed to appear as a brand. Keyed by the stable slug a skill
#: declares in ``publisher.id``. Names match the partner labels the console
#: shows in Settings > Skills (``PARTNER_MARKS`` and the
#: ``webSetCapabilities.skillsPartner*`` strings used by
#: ``frontend/src/web/features/settings/sections/skills.tsx``); ``logo`` stays
#: empty for partners whose mark the client ships as a bundled asset under
#: ``frontend/src/assets``, so no prompt or page has to fetch a remote image to
#: render a card.
RECOGNIZED_PUBLISHERS: dict[str, SkillPublisher] = {
    "robinhood": SkillPublisher(
        id="robinhood",
        name="Robinhood",
        url="https://robinhood.com",
        logo="",
    ),
    "bankr": SkillPublisher(
        id="bankr",
        name="Bankr",
        url="https://github.com/BankrBot/skills",
        logo="",
    ),
    "capminal": SkillPublisher(
        id="capminal",
        name="Capminal",
        url="https://github.com/Capminal/agent-skills",
        logo="",
    ),
    "aeon": SkillPublisher(
        id="aeon",
        name="Aeon",
        url="https://www.aeon.fun",
        logo="",
    ),
    "muse": SkillPublisher(
        id="muse",
        name="Muse",
        url="https://musebook.me",
        logo="",
    ),
}


#: Layers whose ``SKILL.md`` may name its own publisher. Only ``bundled`` skills
#: ship inside the wheel, so their frontmatter is ours and was reviewed with the
#: rest of the release. Anything reachable from a writable skills directory —
#: managed, personal, project, workspace, extra — is operator- or hub-supplied
#: text that must not be able to mint a brand for itself.
SELF_DECLARING_LAYERS: frozenset[SkillLayer] = frozenset({SkillLayer.BUNDLED})


def resolve_publisher(raw: object) -> SkillPublisher:
    """Return the allowlisted publisher a manifest selected, or an empty one.

    ``raw`` is whatever the frontmatter declared — a mapping, a bare id string,
    or junk. Only the ``id`` is read from it; name, url, and logo always come
    from :data:`RECOGNIZED_PUBLISHERS` so a skill cannot mint its own branding.
    """
    if isinstance(raw, SkillPublisher):
        declared_id = raw.id
    elif isinstance(raw, dict):
        declared_id = str(raw.get("id", "") or "")
    elif isinstance(raw, str):
        declared_id = raw
    else:
        return SkillPublisher()

    return RECOGNIZED_PUBLISHERS.get(declared_id.strip().lower(), SkillPublisher())


def resolve_declared_publisher(raw: object, layer: SkillLayer) -> SkillPublisher:
    """Resolve a publisher a *manifest* declared, honouring the layer it sits in.

    Use this for anything read out of a ``SKILL.md`` (or out of the snapshot
    cache, which is a writable file mirroring one). :func:`resolve_publisher` is
    for ids that arrived over a trusted channel instead — today that is the hub
    lockfile, whose slug comes from the catalog row rather than the skill text.
    """
    if layer not in SELF_DECLARING_LAYERS:
        return SkillPublisher()
    return resolve_publisher(raw)
