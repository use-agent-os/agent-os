"""Issues #3163 and #3184: parsed multipart forms were never closed.

Starlette spools every uploaded part to a ``SpooledTemporaryFile``. The
handler owns the parsed form, and neither
``POST /api/v1/files/upload`` nor ``POST /api/audio/transcribe`` closed it --
not on success and not on any of the several early returns (a rejected mime,
an empty or oversize upload, a store or provider failure). The descriptor and
its temporary file then survive the request, accumulating for the life of the
process under upload traffic.

Both endpoints are covered here because they are one root cause with one
fix, following the maintainer guidance on #2928 / #2930.

The assertion is made on the real request path rather than on a stub: the
handler's own ``form()`` is wrapped so the test holds the very ``FormData``
the handler used, and asks it afterwards whether its files were closed.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("starlette.testclient")

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from agentos.gateway.config import GatewayConfig  # noqa: E402

_PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64

# auth.mode=none admits only a loopback peer (TestClient's default is "testclient").
_LOOPBACK_PEER = ("127.0.0.1", 50000)


class _FormSpy:
    """Records the forms a handler parses, and whether their files closed."""

    def __init__(self) -> None:
        self.forms: list[Any] = []

    def all_closed(self) -> bool:
        assert self.forms, "the handler never parsed a form"
        for form in self.forms:
            for value in form.values():
                file = getattr(value, "file", None)
                if file is not None and not file.closed:
                    return False
        return True


def _spy_on_forms(monkeypatch: pytest.MonkeyPatch, module: Any) -> _FormSpy:
    """Wrap ``module.bounded_request`` so parsed forms are observable."""
    spy = _FormSpy()
    original = module.bounded_request

    def bounded_request(request: Any, budget: int) -> Any:
        wrapped = original(request, budget)
        original_form = wrapped.form

        async def form(*args: Any, **kwargs: Any) -> Any:
            parsed = await original_form(*args, **kwargs)
            spy.forms.append(parsed)
            return parsed

        wrapped.form = form  # type: ignore[method-assign]
        return wrapped

    monkeypatch.setattr(module, "bounded_request", bounded_request)
    return spy


# ---------------------------------------------------------------------------
# POST /api/v1/files/upload  (#3163)
# ---------------------------------------------------------------------------


def _upload_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _FormSpy]:
    from agentos.gateway import uploads as uploads_module
    from agentos.gateway.uploads import UploadStore, register_upload_routes

    spy = _spy_on_forms(monkeypatch, uploads_module)
    store = UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    app = Starlette(debug=False)
    register_upload_routes(app, config=GatewayConfig(), store=store)
    return TestClient(app, client=_LOOPBACK_PEER), spy


def test_upload_closes_the_form_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client, spy = _upload_client(monkeypatch)

    with client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("a.png", _PNG, "image/png")},
        )

    assert response.status_code == 200
    assert spy.all_closed()


def test_upload_closes_the_form_when_the_part_is_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.gateway.attachment_ingest import TEXT_ATTACHMENT_BYTES

    client, spy = _upload_client(monkeypatch)

    with client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("big.txt", b"a" * (TEXT_ATTACHMENT_BYTES + 1), "text/plain")},
        )

    assert response.status_code == 413
    assert spy.all_closed()


def test_upload_closes_the_form_when_the_mime_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, spy = _upload_client(monkeypatch)

    with client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("x.bin", b"payload", "application/x-not-a-thing")},
        )

    assert response.status_code in {400, 415}
    assert spy.all_closed()


def test_upload_closes_the_form_when_the_upload_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, spy = _upload_client(monkeypatch)

    with client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("empty.png", b"", "image/png")},
        )

    assert response.status_code == 400
    assert spy.all_closed()


def test_upload_closes_the_form_when_the_store_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside ``store.put`` must not skip the release either."""
    from agentos.gateway import uploads as uploads_module
    from agentos.gateway.uploads import (
        UploadStore,
        UploadUnsupportedMimeError,
        register_upload_routes,
    )

    spy = _spy_on_forms(monkeypatch, uploads_module)
    store = UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)

    async def put(*_args: Any, **_kwargs: Any) -> str:
        raise UploadUnsupportedMimeError("nope")

    monkeypatch.setattr(store, "put", put)
    app = Starlette(debug=False)
    register_upload_routes(app, config=GatewayConfig(), store=store)

    with TestClient(app, client=_LOOPBACK_PEER) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("a.png", _PNG, "image/png")},
        )

    assert response.status_code == 415
    assert spy.all_closed()


# ---------------------------------------------------------------------------
# POST /api/audio/transcribe  (#3184)
# ---------------------------------------------------------------------------


def _audio_config() -> GatewayConfig:
    config = GatewayConfig()
    config.audio.enabled = True
    return config


def test_transcribe_closes_the_form_when_the_mime_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.gateway import audio_transcription as audio_module
    from agentos.gateway.audio_transcription import register_audio_transcription_routes

    spy = _spy_on_forms(monkeypatch, audio_module)
    app = Starlette(debug=False)
    register_audio_transcription_routes(app, config=_audio_config())

    with TestClient(app, client=_LOOPBACK_PEER) as client:
        response = client.post(
            "/api/audio/transcribe",
            files={"file": ("note.txt", b"not audio", "text/plain")},
        )

    assert response.status_code == 415
    assert spy.all_closed()


def test_transcribe_closes_the_form_when_the_upload_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.gateway import audio_transcription as audio_module
    from agentos.gateway.audio_transcription import register_audio_transcription_routes

    spy = _spy_on_forms(monkeypatch, audio_module)
    app = Starlette(debug=False)
    register_audio_transcription_routes(app, config=_audio_config())

    with TestClient(app, client=_LOOPBACK_PEER) as client:
        response = client.post(
            "/api/audio/transcribe",
            files={"file": ("voice.webm", b"", "audio/webm")},
        )

    assert response.status_code == 400
    assert spy.all_closed()


def test_transcribe_closes_the_form_when_the_provider_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider call is the longest path through the handler and the one
    most likely to raise; it must release the spooled audio too."""
    from agentos.gateway import audio_transcription as audio_module
    from agentos.gateway.audio_transcription import register_audio_transcription_routes

    spy = _spy_on_forms(monkeypatch, audio_module)

    async def transcribe_audio_bytes(**_kwargs: Any) -> Any:
        raise RuntimeError("provider is down")

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes", transcribe_audio_bytes)
    app = Starlette(debug=False)
    register_audio_transcription_routes(app, config=_audio_config())

    with TestClient(app, client=_LOOPBACK_PEER) as client:
        response = client.post(
            "/api/audio/transcribe",
            files={"file": ("voice.webm", b"audio-bytes", "audio/webm")},
        )

    assert response.status_code == 502
    assert spy.all_closed()
