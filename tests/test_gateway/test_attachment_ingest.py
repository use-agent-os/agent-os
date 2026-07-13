from __future__ import annotations

import base64

import pytest

from agentos.gateway.attachment_ingest import ingest_attachments


@pytest.mark.asyncio
async def test_channel_bytes_attachment_normalizes_to_engine_shape() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "note.txt", "mime_type": "text/plain", "data": b"hello"}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
    )

    assert result.text == "read it"
    assert result.failures == []
    assert result.attachments == [
        {
            "name": "note.txt",
            "type": "text/plain",
            "data": base64.b64encode(b"hello").decode("ascii"),
            "_was_staged": True,
        }
    ]


@pytest.mark.asyncio
async def test_url_only_channel_attachment_degrades_with_marker() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "remote.pdf", "mime_type": "application/pdf", "url": "https://example.test/x.pdf"}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
    )

    assert result.attachments == []
    assert result.failures[0].reason == "missing_data"
    assert "[attachment unavailable: remote.pdf: missing_data]" in result.text


@pytest.mark.asyncio
async def test_download_failure_raises_in_rpc_mode() -> None:
    with pytest.raises(ValueError, match="download_failed: boom"):
        await ingest_attachments(
            "read it",
            [{"name": "remote.pdf", "mime_type": "application/pdf", "_ingest_error": "boom"}],
            failure_mode="raise",
        )


@pytest.mark.asyncio
async def test_failure_marker_sanitizes_attachment_name() -> None:
    result = await ingest_attachments(
        "read it",
        [{"name": "bad\nname.pdf", "mime_type": "application/pdf", "_ingest_error": "boom"}],
        failure_mode="mark",
    )

    assert "[attachment unavailable: bad name.pdf: download_failed]" in result.text
