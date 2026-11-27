"""A published artifact reaches the web console live as ``session.event.artifact``.

The engine builds its ``ArtifactEvent`` from the artifact ref's payload, so the
event carries the ref's own ``kind`` (``artifact_ref``, which artifact payloads
keep: history rows, ``agentos agent run --json``). The gateway named the
stream frame after that kind; every client, the live-turn snapshot and the
text-segment boundary listen for ``session.event.artifact``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gateway_turn_rig import turn_rig

from agentos.artifacts import ArtifactStore
from agentos.engine.types import ArtifactEvent, DoneEvent, TextDeltaEvent


class _PublishingRunner:
    def __init__(self, ref: dict[str, Any]) -> None:
        self.ref = ref

    async def run(self, message: str, session_key: str, **kwargs: Any):  # noqa: ANN201
        yield TextDeltaEvent(text="Charting")
        # What the engine yields for a publish_artifact result.
        yield ArtifactEvent(**self.ref)
        yield DoneEvent(text="Charting")


async def test_a_live_artifact_streams_as_session_event_artifact(tmp_path: Path) -> None:
    ref = ArtifactStore(tmp_path / "media").publish_bytes(
        b'{"type":"cards","cards":[]}',
        session_id="s1",
        session_key="agent:main:webchat:t",
        name="cards.json",
        mime="application/vnd.agentos.cards+json",
        source="publish_artifact",
    )
    async with turn_rig(tmp_path, _PublishingRunner(ref.to_dict())) as rig:
        task_id = await rig.enqueue()
        await rig.runtime.wait(task_id, timeout=3)

    names = rig.live.names()
    assert "session.event.artifact_ref" not in names
    frames = [payload for name, payload, _ in rig.live.events if name == "session.event.artifact"]
    assert len(frames) == 1
    assert frames[0]["id"] == ref.id
    assert frames[0]["download_url"] == f"/api/v1/artifacts/{ref.id}"
    assert names.index("session.event.artifact") < names.index("session.event.done")
