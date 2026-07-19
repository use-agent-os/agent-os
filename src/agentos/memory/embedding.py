"""Embedding provider abstraction and implementations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import httpx
import structlog

from agentos.env import trust_env as _trust_env
from agentos.provider.openrouter_attribution import openrouter_app_headers

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    import numpy as np

EMBEDDING_BATCH_MAX_BYTES = 8000
BATCH_FAILURE_LIMIT = 2
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_MS = 500
DEFAULT_RETRY_MAX_MS = 8000


@dataclass(frozen=True)
class LocalModelSpec:
    """Per-model metadata for local (ONNX) embedding models.

    ``dims`` is ``None`` when the dimensionality should be discovered at
    load time rather than asserted up front.
    """

    model_id: str
    query_prefix: str
    document_prefix: str
    pooling: str  # "cls" | "mean"
    max_tokens: int
    dims: int | None


LOCAL_MODEL_SPECS: dict[str, LocalModelSpec] = {
    "BAAI/bge-small-zh-v1.5": LocalModelSpec(
        model_id="BAAI/bge-small-zh-v1.5",
        query_prefix="",
        document_prefix="",
        pooling="cls",
        max_tokens=512,
        dims=None,
    ),
    "google/embeddinggemma-300m": LocalModelSpec(
        model_id="google/embeddinggemma-300m",
        query_prefix="task: search result | query: ",
        document_prefix="title: none | text: ",
        pooling="mean",
        max_tokens=1024,
        dims=768,
    ),
    # Pilot router backbone (owner-locked). Attention-mask mean pooling, 256
    # tokens, right truncation, no text prefix. Registered explicitly so the
    # Pilot feature builder never rides on the unknown-model (CLS/no-prefix)
    # default. L2 normalization is applied by the Pilot feature builder, not
    # here — the provider returns raw vectors.
    "sentence-transformers/all-MiniLM-L6-v2": LocalModelSpec(
        model_id="sentence-transformers/all-MiniLM-L6-v2",
        query_prefix="",
        document_prefix="",
        pooling="mean",
        max_tokens=256,
        dims=384,
    ),
}

_DEFAULT_MODEL_SPEC = LocalModelSpec(
    model_id="",
    query_prefix="",
    document_prefix="",
    pooling="cls",
    max_tokens=512,
    dims=None,
)

# Ollama tags don't carry HF-style org/name ids, so map the ones we know
# apply prompt prefixes to the canonical spec id. The lookup key is the
# model name with any ":tag" suffix stripped first (see ``_spec_id``), so
# "embeddinggemma:latest", "embeddinggemma:300m", and bare "embeddinggemma"
# all resolve to the same alias. Any other Ollama model (e.g.
# "nomic-embed-text" or "nomic-embed-text:latest") falls through to the
# no-prefix default.
_OLLAMA_SPEC_ALIASES: dict[str, str] = {
    "embeddinggemma": "google/embeddinggemma-300m",
}


def model_spec(model_id: str) -> LocalModelSpec:
    """Spec for model_id; unknown ids get a BGE-like default (cls/512/no prefixes)."""
    return LOCAL_MODEL_SPECS.get(model_id, _DEFAULT_MODEL_SPEC)


def format_query_text(model_id: str, text: str) -> str:
    return model_spec(model_id).query_prefix + text


def format_document_text(model_id: str, text: str) -> str:
    return model_spec(model_id).document_prefix + text

RETRYABLE_ERROR_PATTERNS = [
    "rate_limit",
    "429",
    "too many requests",
    "500",
    "502",
    "503",
    "504",
    "cloudflare",
]


def _is_retryable(error_msg: str) -> bool:
    msg = error_msg.lower()
    return any(p in msg for p in RETRYABLE_ERROR_PATTERNS)


async def _retry_with_backoff(
    fn,
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    base_ms: int = DEFAULT_RETRY_BASE_MS,
    max_ms: int = DEFAULT_RETRY_MAX_MS,
):
    import asyncio

    last_error = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1 and _is_retryable(str(e)):
                delay = min(base_ms * (2**attempt), max_ms) / 1000.0
                logger.warning("embedding_retry", attempt=attempt + 1, delay=delay, error=str(e))
                await asyncio.sleep(delay)
            else:
                raise
    raise last_error  # type: ignore[misc]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    @property
    def provider_id(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query text."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts, returning one vector per text."""
        ...

    async def probe(self) -> tuple[bool, str | None]:
        """Check if this provider is available. Returns (ok, error)."""
        ...


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embedding provider (also works with OpenRouter)."""

    DEFAULT_MODEL = "text-embedding-3-small"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        extra_headers: dict[str, str] | None = None,
        dimensions: int | None = None,
        timeout_query_ms: int = 60_000,
        timeout_batch_ms: int = 120_000,
    ) -> None:
        self._api_key = api_key
        self._model = model or self.DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._extra_headers = dict(extra_headers or {})
        self._dimensions = dimensions
        self._timeout_query = timeout_query_ms / 1000.0
        self._timeout_batch = timeout_batch_ms / 1000.0

    @property
    def provider_id(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        headers.update(openrouter_app_headers(self._base_url))
        headers.update(self._extra_headers)
        return headers

    def _payload(self, input_value: str | list[str]) -> dict[str, object]:
        payload: dict[str, object] = {"input": input_value, "model": self._model}
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions
        return payload

    async def embed_query(self, text: str) -> list[float]:
        async def _call():
            async with httpx.AsyncClient(trust_env=_trust_env()) as client:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers(),
                    json=self._payload(text),
                    timeout=self._timeout_query,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"]

        return await _retry_with_backoff(_call)  # type: ignore[no-any-return]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        async def _call():
            async with httpx.AsyncClient(trust_env=_trust_env()) as client:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers(),
                    json=self._payload(texts),
                    timeout=self._timeout_batch,
                )
                resp.raise_for_status()
                data = resp.json()
                # API returns results in order
                results = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in results]

        return await _retry_with_backoff(_call)  # type: ignore[no-any-return]

    async def probe(self) -> tuple[bool, str | None]:
        try:
            await self.embed_query("probe")
            return True, None
        except Exception as e:
            return False, str(e)


class OllamaEmbeddingProvider:
    """Ollama local embedding provider."""

    DEFAULT_MODEL = "embeddinggemma"

    def __init__(
        self,
        model: str | None = None,
        base_url: str = "http://localhost:11434",
        timeout_query_ms: int = 300_000,
        timeout_batch_ms: int = 600_000,
    ) -> None:
        self._model = model or self.DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._timeout_query = timeout_query_ms / 1000.0
        self._timeout_batch = timeout_batch_ms / 1000.0

    @property
    def provider_id(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    @property
    def _spec_id(self) -> str:
        """Canonical spec id for prompt-prefix lookups. Ollama tags don't
        carry HF-style org/name ids, so known prefix-capable tags are
        mapped via ``_OLLAMA_SPEC_ALIASES``; anything else (e.g.
        ``nomic-embed-text``) gets the no-prefix default.

        The ``:tag`` suffix (e.g. ``:latest``, ``:300m``) is stripped
        before the alias lookup so any tag of a known model (not just the
        untagged name) resolves to its prefix-capable spec."""
        base = self._model.split(":", 1)[0]
        return _OLLAMA_SPEC_ALIASES.get(base, self._model)

    async def _embed_raw(self, text: str) -> list[float]:
        async def _call():
            async with httpx.AsyncClient(trust_env=_trust_env()) as client:
                resp = await client.post(
                    f"{self._base_url}/api/embeddings",
                    json={"model": self._model, "prompt": text},
                    timeout=self._timeout_query,
                )
                resp.raise_for_status()
                return resp.json()["embedding"]

        return await _retry_with_backoff(_call)  # type: ignore[no-any-return]

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed_raw(format_query_text(self._spec_id, text))

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            vec = await self._embed_raw(format_document_text(self._spec_id, text))
            results.append(vec)
        return results

    async def probe(self) -> tuple[bool, str | None]:
        try:
            await self.embed_query("probe")
            return True, None
        except Exception as e:
            return False, str(e)


class NullEmbeddingProvider:
    """Placeholder when no real embedding provider is configured. FTS-only mode."""

    @property
    def provider_id(self) -> str:
        return "none"

    @property
    def model(self) -> str:
        return "fts-only"

    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("NullEmbeddingProvider cannot produce embeddings")

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("NullEmbeddingProvider cannot produce embeddings")

    async def probe(self) -> tuple[bool, str | None]:
        return False, "no embedding provider configured"


class LocalEmbeddingProvider:
    """Local embedding provider — ONNX-only backend.

    Loads the bundled BGE ONNX export
    (``memory/models/bge_onnx/``) via
    ``onnxruntime`` + the Hugging Face ``tokenizers`` runtime. There is no
    sentence-transformers / torch path: the project ships an INT8 BGE
    ONNX so the FP32 sentence-transformers download is unnecessary
    weight, and earlier dual-backend coexistence created a real
    train/inference distribution mismatch when callers silently
    swapped between FP32 and INT8.

    Produces raw (un-normalised) float32 vectors of shape ``(N, dim)``;
    downstream callers (e.g. ``SemanticIndex``) normalise or reduce
    themselves.

    Failure modes:
      * ``onnxruntime`` or ``tokenizers`` not installed → ``ImportError``
      * bundled ONNX dir / weights missing → ``RuntimeError``
      * runtime error during model load → ``RuntimeError``

    All raise on first ``encode_sync`` (lazy load); construction itself
    is side-effect-free so importers can probe ``loaded`` first.
    """

    DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

    _BUNDLED_ONNX_DIR = Path(__file__).resolve().parent / "models" / "bge_onnx"

    @classmethod
    def resolve_onnx_dir(cls, model_name: str) -> Path | None:
        """Resolve the ONNX dir for the given model.

        Resolution order:
          1. The bundled-export convention (unchanged for BGE; also covers
             any model shipped under ``models/embeddings/{short}-int8``).
          2. A previously downloaded model dir under the user's AgentOS home
             (``agentos.memory.model_download.downloaded_model_dir``).

        Returns ``None`` when neither source has the model.
        """
        if model_name == cls.DEFAULT_MODEL and cls._BUNDLED_ONNX_DIR.is_dir():
            return cls._BUNDLED_ONNX_DIR
        short = model_name.split("/")[-1]
        candidate = cls._BUNDLED_ONNX_DIR.parent / "embeddings" / f"{short}-int8"
        if candidate.is_dir():
            return candidate
        # Lazy import: model_download pulls in httpx-based download plumbing
        # that has no business being an import-time dependency of the
        # embedding module, and would risk a cycle if that ever changed.
        from agentos.memory import model_download

        return model_download.downloaded_model_dir(model_name)

    @classmethod
    def _bundled_onnx_dir(cls, model_name: str) -> Path | None:
        """Deprecated alias for :meth:`resolve_onnx_dir`, kept for external
        callers (``embedding_resolver.local_bge_available``,
        ``gateway.boot``) and existing test monkeypatches."""
        return cls.resolve_onnx_dir(model_name)

    def __init__(
        self,
        model_name: str | None = None,
        *,
        onnx_dir: Path | str | None = "auto",
    ) -> None:
        import threading

        self._model_name = model_name or self.DEFAULT_MODEL
        # ONNX dir resolution. Two contract values:
        #   "auto" (default) → convention-based bundled discovery
        #   <path>           → use exactly this dir
        # ``None`` is rejected explicitly: the previous semantics
        # (force sentence-transformers fallback) no longer exists.
        self._onnx_dir_explicit = onnx_dir != "auto"
        if onnx_dir == "auto":
            self._onnx_dir: Path | None = self.resolve_onnx_dir(self._model_name)
        elif onnx_dir is None:
            raise ValueError(
                "onnx_dir=None is no longer supported; the sentence-transformers "
                "fallback was removed. Pass an explicit ONNX directory or omit "
                'the argument to use the bundled "auto" discovery.'
            )
        else:
            self._onnx_dir = Path(onnx_dir).expanduser().resolve()
        self._dim: int | None = None
        self._loaded: bool = False
        # ONNX backend state — populated on first encode.
        self._onnx_session: Any | None = None
        self._onnx_tokenizer: Any | None = None
        self._onnx_input_names: list[str] = []
        # Guards _ensure_loaded so concurrent first-time encode_sync calls
        # don't each construct their own session. Without this,
        # get_embedder()'s singleton guarantee only covers the provider
        # object — the underlying weights would still load twice.
        self._load_lock = threading.Lock()

    @property
    def provider_id(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def backend(self) -> str | None:
        """Always ``"onnx"`` after the first successful encode, ``None``
        before. Kept as an attribute (rather than a constant) so existing
        callers/tests can probe whether the provider has actually loaded."""
        return "onnx" if self._loaded else None

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise RuntimeError("LocalEmbeddingProvider not loaded yet")
        return self._dim

    def _ensure_loaded(self) -> None:
        # Double-checked locking: the fast path is unlocked so subsequent
        # calls don't pay a lock acquisition; only the first one (or
        # several concurrent firsts) pays the lock and the load.
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self._load_onnx()
            self._loaded = True

    def _load_onnx(self) -> None:
        """Load the ONNX backend. Raises with an actionable message on
        any failure — there is no fallback to retry against."""
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ImportError(
                "LocalEmbeddingProvider requires `onnxruntime` and "
                "`tokenizers`. Install via "
                "`uv sync --extra recommended`."
            ) from exc
        if self._onnx_dir is None or not self._onnx_dir.is_dir():
            raise RuntimeError(
                f"LocalEmbeddingProvider could not locate a bundled ONNX dir "
                f"for model {self._model_name!r}; got onnx_dir={self._onnx_dir!r}. "
                "The bundled BGE export should ship at "
                "src/agentos/memory/models/bge_onnx/."
            )
        onnx_files = sorted(self._onnx_dir.glob("*.onnx"))
        if not onnx_files:
            raise RuntimeError(
                f"LocalEmbeddingProvider found onnx_dir={self._onnx_dir} but "
                "it contains no *.onnx files."
            )
        spec = model_spec(self._model_name)
        if self._onnx_dir_explicit and spec is _DEFAULT_MODEL_SPEC:
            logger.warning(
                "local_embedding.default_spec_for_custom_dir",
                model_name=self._model_name,
                onnx_dir=str(self._onnx_dir),
                assumed_pooling=_DEFAULT_MODEL_SPEC.pooling,
                assumed_max_tokens=_DEFAULT_MODEL_SPEC.max_tokens,
                assumed_prefixes=False,
            )
        try:
            session = ort.InferenceSession(str(onnx_files[0]), providers=["CPUExecutionProvider"])
            tokenizer_path = self._onnx_dir / "tokenizer.json"
            if not tokenizer_path.is_file():
                raise RuntimeError(f"tokenizer.json not found in {self._onnx_dir}")
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
            tokenizer.enable_truncation(max_length=spec.max_tokens)
            tokenizer.enable_padding()
            input_names = [inp.name for inp in session.get_inputs()]
            # Warm-up + dim discovery via a dummy encode.
            self._onnx_tokenizer = tokenizer
            feed = self._tokenize_onnx(["warmup"], input_names)
            out = session.run(None, feed)[0]
            if out.ndim == 3:
                out = self._pool(out, feed, spec.pooling)
        except Exception as exc:
            logger.warning("local_embedding.onnx_load_failed", error=str(exc))
            raise RuntimeError(
                f"LocalEmbeddingProvider failed to initialise ONNX session "
                f"from {onnx_files[0]}: {exc}"
            ) from exc
        self._onnx_session = session
        self._onnx_tokenizer = tokenizer
        self._onnx_input_names = input_names
        self._dim = int(out.shape[-1])

    def _tokenize_onnx(self, texts: list[str], input_names: list[str]) -> dict[str, Any]:
        import numpy as np

        assert self._onnx_tokenizer is not None
        encodings = self._onnx_tokenizer.encode_batch(texts)  # type: ignore[union-attr]
        arrays: dict[str, Any] = {
            "input_ids": np.asarray([enc.ids for enc in encodings], dtype=np.int64),
            "attention_mask": np.asarray(
                [enc.attention_mask for enc in encodings],
                dtype=np.int64,
            ),
            "token_type_ids": np.asarray([enc.type_ids for enc in encodings], dtype=np.int64),
        }
        return {name: value for name, value in arrays.items() if name in input_names}

    @staticmethod
    def _pool(outputs: Any, feed: dict[str, Any], pooling: str) -> Any:
        """Reduce an ndim==3 ``last_hidden_state``-shaped output to
        ``(batch, dim)``.

        ``"cls"`` takes the first-token vector (BGE-style models). ``"mean"``
        computes an attention-mask-weighted mean over the sequence axis
        (EmbeddingGemma and other mean-pooling models): positions where the
        mask is 0 (padding) don't contribute to the sum, and the divisor is
        clamped to at least 1 to avoid dividing by zero for an all-masked
        (empty) sequence.
        """
        import numpy as np

        if pooling == "mean":
            mask = feed["attention_mask"].astype(np.float32)  # (batch, seq)
            mask_expanded = mask[..., None]  # (batch, seq, 1)
            summed = np.sum(outputs * mask_expanded, axis=1)  # (batch, dim)
            counts = np.clip(np.sum(mask, axis=1, keepdims=True), a_min=1.0, a_max=None)
            return (summed / counts).astype(np.float32)
        return outputs[:, 0, :]  # CLS pooling

    def encode_sync(
        self,
        texts: list[str],
        *,
        batch_size: int = 32,
        show_progress_bar: bool = False,  # accepted for API compat; unused
        role: str | None = None,
    ) -> np.ndarray:  # noqa: F821
        """Encode ``texts`` to embedding vectors.

        ``role`` is ``None`` by default, meaning texts are encoded as-is
        (raw) — this is the path used by skills' ``SemanticIndex``, which
        must not change behavior this task. ``embed_query``/``embed_batch``
        pre-format their text with the model's prompt prefix (if any)
        before calling this method, so ``role`` itself is currently
        informational only; it exists for future callers that want the
        provider to apply prefixes on their behalf.
        """
        del role  # reserved for future use; formatting happens in embed_*
        self._ensure_loaded()
        return self._encode_onnx(list(texts), batch_size=batch_size)

    def _encode_onnx(self, texts: list[str], *, batch_size: int) -> np.ndarray:  # noqa: F821
        import numpy as np

        assert self._onnx_session is not None
        assert self._onnx_tokenizer is not None
        if not texts:
            dim = self._dim or 0
            return np.zeros((0, dim), dtype=np.float32)
        pooling = model_spec(self._model_name).pooling
        chunks: list[np.ndarray] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            feed = self._tokenize_onnx(batch, self._onnx_input_names)
            outputs = self._onnx_session.run(None, feed)[0]  # type: ignore[union-attr]
            if outputs.ndim == 3:
                outputs = self._pool(outputs, feed, pooling)
            chunks.append(np.asarray(outputs, dtype=np.float32))
        return np.concatenate(chunks, axis=0)

    async def embed_query(self, text: str) -> list[float]:
        import asyncio

        formatted = format_query_text(self._model_name, text)
        arr = await asyncio.to_thread(self.encode_sync, [formatted])
        return cast(list[float], arr[0].tolist())

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        formatted = [format_document_text(self._model_name, text) for text in texts]
        arr = await asyncio.to_thread(self.encode_sync, formatted)
        return [row.tolist() for row in arr]

    async def probe(self) -> tuple[bool, str | None]:
        try:
            await self.embed_query("probe")
            return True, None
        except Exception as e:  # ImportError or model load failure
            return False, str(e)


_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0x3400, 0x4DBF),  # CJK Extension A
    (0xF900, 0xFAFF),  # CJK Compatibility
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _estimate_tokens(text: str) -> int:
    """CJK chars ≈ 1 token each; ASCII ≈ 4 chars per token."""
    if not text:
        return 0
    cjk_count = sum(1 for ch in text if _is_cjk(ch))
    ascii_count = len(text) - cjk_count
    return max(1, cjk_count + ascii_count // 4)


def chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def chunk_text(
    text: str, chunk_tokens: int = 400, chunk_overlap: int = 50
) -> list[tuple[int, int, str]]:
    """
    Split text into overlapping chunks by approximate token count.
    Returns list of (start_line, end_line, chunk_text).
    CJK chars count as 1 token each; ASCII uses 4 chars per token.
    """
    lines = text.splitlines(keepends=True)
    chunks: list[tuple[int, int, str]] = []

    current_tokens = 0
    chunk_start_line = 0

    i = 0
    while i < len(lines):
        line_tokens = _estimate_tokens(lines[i])
        if current_tokens + line_tokens > chunk_tokens and current_tokens > 0:
            # emit chunk
            chunk_text_val = "".join(lines[chunk_start_line:i]).strip()
            if chunk_text_val:
                chunks.append((chunk_start_line + 1, i, chunk_text_val))

            # move back by overlap
            overlap_tokens_back = 0
            new_start = i
            for j in range(i - 1, chunk_start_line - 1, -1):
                overlap_tokens_back += _estimate_tokens(lines[j])
                if overlap_tokens_back >= chunk_overlap:
                    new_start = j
                    break
            chunk_start_line = new_start
            current_tokens = sum(_estimate_tokens(lines[k]) for k in range(chunk_start_line, i))
        current_tokens += line_tokens
        i += 1

    # final chunk
    if chunk_start_line < len(lines):
        chunk_text_val = "".join(lines[chunk_start_line:]).strip()
        if chunk_text_val:
            chunks.append((chunk_start_line + 1, len(lines), chunk_text_val))

    return chunks if chunks else [(1, len(lines), text.strip())]
