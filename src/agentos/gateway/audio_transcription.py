"""HTTP route for WebUI microphone transcription."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any, cast

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentos.gateway.config import GatewayConfig
from agentos.gateway.uploads import _extract_authorization_token
from agentos.provider.audio import (
    ElevenLabsAudioProductionProvider,
    ElevenLabsSpeechToTextRequest,
    ElevenLabsSpeechToTextResult,
)

log = structlog.get_logger(__name__)

MAX_TRANSCRIPTION_BYTES = 30 * 1024 * 1024
_MAX_TRANSCRIPTION_BYTES = MAX_TRANSCRIPTION_BYTES


def _default_provider_factory(config: GatewayConfig) -> ElevenLabsAudioProductionProvider:
    provider_cfg = config.audio.providers.elevenlabs
    return ElevenLabsAudioProductionProvider(
        api_key=getattr(provider_cfg, "api_key", ""),
        api_key_env=getattr(provider_cfg, "api_key_env", "ELEVENLABS_API_KEY"),
        base_url=getattr(provider_cfg, "base_url", "https://api.elevenlabs.io"),
    )


async def transcribe_audio_bytes(
    config: GatewayConfig,
    payload: bytes,
    *,
    filename: str = "voice.webm",
    mime_type: str = "audio/webm",
    model_id: str | None = None,
    language_code: str | None = None,
    provider_factory: Callable[[GatewayConfig], Any] = _default_provider_factory,
) -> ElevenLabsSpeechToTextResult:
    """Helper to call speech-to-text using the configured provider."""
    if not getattr(config.audio, "enabled", False):
        raise RuntimeError("Audio transcription is disabled")

    provider_cfg = config.audio.providers.elevenlabs
    actual_model_id = (
        model_id or getattr(provider_cfg, "speech_to_text_model", "scribe_v2") or "scribe_v2"
    )

    provider = cast(ElevenLabsAudioProductionProvider, provider_factory(config))
    return await provider.transcribe_audio(
        ElevenLabsSpeechToTextRequest(
            audio_bytes=payload,
            filename=filename,
            mime_type=mime_type,
            model_id=actual_model_id,
            language_code=language_code or None,
        )
    )


def register_audio_transcription_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    provider_factory: Callable[[GatewayConfig], Any] = _default_provider_factory,
) -> None:
    """Register POST /api/audio/transcribe for browser-recorded audio."""

    async def transcribe_handler(request: Request) -> JSONResponse:
        if config.auth.mode == "token":
            if config.auth.token and _extract_authorization_token(request) != config.auth.token:
                return JSONResponse(
                    {
                        "error": (
                            "Authorization header (Bearer ...) required for /api/audio/transcribe"
                        ),
                        "code": "UNAUTHORIZED",
                    },
                    status_code=401,
                )

        if not getattr(config.audio, "enabled", False):
            return JSONResponse(
                {"error": "audio transcription is disabled", "code": "UNAVAILABLE"},
                status_code=503,
            )

        try:
            form = await request.form()
        except Exception as exc:
            if config.debug:
                from agentos.redact import redact_sensitive_text

                return JSONResponse(
                    {"error": f"multipart/form-data required: {redact_sensitive_text(str(exc))}"},
                    status_code=400,
                )
            return JSONResponse({"error": "multipart/form-data required"}, status_code=400)

        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "missing 'file' multipart field"}, status_code=400)

        filename = getattr(upload, "filename", None) or "voice.webm"
        mime_type = getattr(upload, "content_type", None) or form.get("mime") or "audio/webm"
        if not isinstance(mime_type, str) or not mime_type.startswith(("audio/", "video/")):
            return JSONResponse(
                {"error": "audio or video upload required", "code": "UNSUPPORTED_MEDIA_TYPE"},
                status_code=415,
            )

        payload = await upload.read()
        if not isinstance(payload, bytes) or len(payload) == 0:
            return JSONResponse({"error": "empty upload"}, status_code=400)
        if len(payload) > _MAX_TRANSCRIPTION_BYTES:
            return JSONResponse(
                {
                    "error": "audio upload exceeds transcription size limit",
                    "code": "TOO_LARGE",
                },
                status_code=413,
            )

        provider_cfg = config.audio.providers.elevenlabs
        model_id = str(
            form.get("model_id")
            or getattr(provider_cfg, "speech_to_text_model", "scribe_v2")
            or "scribe_v2"
        )
        language_code_value = form.get("language_code")
        language_code = language_code_value if isinstance(language_code_value, str) else None

        try:
            result = await transcribe_audio_bytes(
                config=config,
                payload=payload,
                filename=str(filename),
                mime_type=mime_type,
                model_id=model_id,
                language_code=language_code,
                provider_factory=provider_factory,
            )
        except Exception as exc:
            error_id = secrets.token_hex(6)
            log.error(
                "audio.transcription_failed",
                error_id=error_id,
                error=str(exc),
                exc_info=True,
            )
            from agentos.redact import redact_sensitive_text

            if config.debug:
                return JSONResponse(
                    {
                        "error": redact_sensitive_text(str(exc)),
                        "code": "PROVIDER_ERROR",
                        "error_id": error_id,
                    },
                    status_code=502,
                )
            return JSONResponse(
                {
                    "error": "Audio transcription failed",
                    "code": "PROVIDER_ERROR",
                    "error_id": error_id,
                },
                status_code=502,
            )

        response: dict[str, Any] = {
            "text": result.text,
            "provider": result.provider,
            "model": result.model,
        }
        if result.language_code is not None:
            response["language_code"] = result.language_code
        if result.language_probability is not None:
            response["language_probability"] = result.language_probability
        return JSONResponse(response)

    app.router.routes.append(Route("/api/audio/transcribe", transcribe_handler, methods=["POST"]))
