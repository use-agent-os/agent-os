"""Runtime helpers for artifact delivery backstops."""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentos.artifacts import (
    DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactBudgetError,
    ArtifactStore,
    artifact_payload,
    ensure_file_within_budget,
    sha256_file,
)
from agentos.tools.types import ToolContext

log = logging.getLogger(__name__)

_DELIVERABLE_SUFFIXES = frozenset(
    {
        ".csv",
        ".htm",
        ".html",
        ".json",
        ".pdf",
        ".pptx",
        ".tsv",
        ".xlsx",
    }
)
_EXCLUDED_TOP_LEVEL_DIRS = frozenset({".claude", ".codex", ".omx", "memory"})


@dataclass(frozen=True)
class OmittedArtifactPublishResult:
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    failure_summaries: list[str] = field(default_factory=list)


#: A mention has to sit on a filename boundary. Written in terms of filename
#: characters rather than ``\b``: ``.`` and ``-`` are non-word characters, so
#: ``\b`` still matches ``data.json`` inside ``metadata.json``. A trailing
#: ``.`` stays allowed so a name that ends a sentence keeps matching, while a
#: ``.`` followed by an alphanumeric is rejected so ``out.csv`` does not match
#: inside ``out.csv.bak``. ``/`` and ``\`` are boundaries on purpose: a bare
#: name must still match when the text spells the path it sits in.
_MENTION_PREFIX = r"(?<![A-Za-z0-9_.\-])"
_MENTION_SUFFIX = r"(?![A-Za-z0-9_\-])(?!\.[A-Za-z0-9])"


@lru_cache(maxsize=512)
def _mention_pattern(candidate: str) -> re.Pattern[str]:
    return re.compile(_MENTION_PREFIX + re.escape(candidate) + _MENTION_SUFFIX)


def _text_mentions_written_file(final_text: str, record: dict[str, Any]) -> bool:
    """Whether *final_text* names this file, on a filename boundary.

    Plain containment let one filename match inside a longer one, so a reply
    that named only ``metadata.json`` published an intermediate ``data.json``
    the model never mentioned — the opposite of the conservative backstop this
    is meant to be.
    """
    text = final_text.casefold()
    candidates = {
        str(record.get("relative_path") or ""),
        str(record.get("path") or ""),
        str(record.get("name") or ""),
    }
    return any(
        candidate and _mention_pattern(candidate.casefold()).search(text) is not None
        for candidate in candidates
    )


def _published_artifact_keys(ctx: ToolContext) -> set[tuple[str, str]]:
    return {
        (str(artifact.get("sha256")), str(artifact.get("name")))
        for artifact in ctx.published_artifacts
        if artifact.get("sha256") and artifact.get("name")
    }


def auto_publish_omitted_workspace_artifacts(
    ctx: ToolContext | None,
    *,
    final_text: str,
) -> OmittedArtifactPublishResult:
    """Publish deliverable files the model wrote but forgot to publish.

    This is intentionally conservative: a file must be written through a tracked
    workspace file tool during the current turn, have a deliverable suffix, and
    be named in the assistant's final text.
    """

    if ctx is None:
        return OmittedArtifactPublishResult()
    if not (
        ctx.workspace_dir
        and ctx.artifact_media_root
        and ctx.artifact_session_id
        and ctx.session_key
    ):
        return OmittedArtifactPublishResult()

    records = list(getattr(ctx, "workspace_file_writes", []) or [])
    if not records or not final_text.strip():
        return OmittedArtifactPublishResult()

    workspace = Path(ctx.workspace_dir).resolve()
    store = ArtifactStore(ctx.artifact_media_root)
    published: list[dict[str, Any]] = []
    failure_summaries: list[str] = []
    seen_paths: set[Path] = set()
    known_artifact_keys = _published_artifact_keys(ctx)

    for record in records:
        target = Path(str(record.get("path") or "")).expanduser().resolve(strict=False)
        if target in seen_paths:
            continue
        seen_paths.add(target)
        try:
            target.relative_to(workspace)
        except ValueError:
            continue
        relative_path = target.relative_to(workspace)
        if relative_path.parts and relative_path.parts[0] in _EXCLUDED_TOP_LEVEL_DIRS:
            continue
        if target.suffix.casefold() not in _DELIVERABLE_SUFFIXES:
            continue
        if not target.is_file():
            continue
        if not _text_mentions_written_file(final_text, record):
            continue

        max_bytes = (
            ctx.artifact_max_bytes
            if ctx.artifact_max_bytes is not None
            else DEFAULT_ARTIFACT_MAX_BYTES
        )
        try:
            # Size first, from stat(); hashing and publishing only read files
            # that already fit the budget.
            ensure_file_within_budget(target, max_bytes)
            target_sha256 = sha256_file(target)
            artifact_key = (target_sha256, target.name)
            if artifact_key in known_artifact_keys:
                continue
            artifact_mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            existing = store.find_existing_ref(
                session_id=ctx.artifact_session_id,
                session_key=ctx.session_key,
                sha256=target_sha256,
                name=target.name,
                mime=artifact_mime,
            )
            if existing is None:
                ref = store.publish_file(
                    target,
                    session_id=ctx.artifact_session_id,
                    session_key=ctx.session_key,
                    name=target.name,
                    mime=artifact_mime,
                    source="auto_publish_omitted",
                    max_bytes=max_bytes,
                    disk_budget_bytes=ctx.artifact_disk_budget_bytes
                    if ctx.artifact_disk_budget_bytes is not None
                    else DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
                )
            else:
                ref = existing
            payload = artifact_payload(ref)
            ctx.published_artifacts.append(payload)
            published.append(payload)
            known_artifact_keys.add(artifact_key)
        except (ArtifactBudgetError, OSError, ValueError) as exc:
            failure_summaries.append(f"auto-publish failed for {target.name}: {exc}")
            log.warning(
                "artifact_delivery.auto_publish_failed path=%s error=%s",
                str(target),
                exc,
            )
    return OmittedArtifactPublishResult(
        artifacts=published,
        failure_summaries=failure_summaries,
    )
