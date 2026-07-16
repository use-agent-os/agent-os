"""Per-model local embedding specs and Ollama default."""
from pathlib import Path

from agentos.memory.embedding import (
    LocalEmbeddingProvider,
    OllamaEmbeddingProvider,
    format_document_text,
    format_query_text,
    model_spec,
)


def test_gemma_spec_prefixes_and_pooling():
    spec = model_spec("google/embeddinggemma-300m")
    assert spec.pooling == "mean"
    assert spec.dims == 768
    assert format_query_text("google/embeddinggemma-300m", "hi") == (
        "task: search result | query: hi"
    )
    assert format_document_text("google/embeddinggemma-300m", "doc") == (
        "title: none | text: doc"
    )


def test_bge_spec_is_unchanged_behavior():
    spec = model_spec("BAAI/bge-small-zh-v1.5")
    assert spec.pooling == "cls"
    assert spec.max_tokens == 512
    assert format_query_text("BAAI/bge-small-zh-v1.5", "hi") == "hi"


def test_unknown_model_gets_no_prefix_default():
    assert format_query_text("some/other-model", "x") == "x"


def test_ollama_default_model_is_embeddinggemma():
    assert OllamaEmbeddingProvider.DEFAULT_MODEL == "embeddinggemma"


async def test_ollama_gemma_applies_prefixes(monkeypatch):
    provider = OllamaEmbeddingProvider(model="embeddinggemma")
    seen: list[str] = []

    async def fake_embed(text: str) -> list[float]:
        seen.append(text)
        return [0.0]

    monkeypatch.setattr(provider, "_embed_raw", fake_embed)
    await provider.embed_query("q")
    await provider.embed_batch(["d1", "d2"])
    assert seen[0] == "task: search result | query: q"
    assert seen[1] == "title: none | text: d1"


async def test_ollama_non_gemma_model_unprefixed(monkeypatch):
    provider = OllamaEmbeddingProvider(model="nomic-embed-text")
    seen: list[str] = []

    async def fake_embed(text: str) -> list[float]:
        seen.append(text)
        return [0.0]

    monkeypatch.setattr(provider, "_embed_raw", fake_embed)
    await provider.embed_query("q")
    assert seen == ["q"]


class _StubEncoding:
    def __init__(self, ids: list[int], attention_mask: list[int]) -> None:
        self.ids = ids
        self.attention_mask = attention_mask
        self.type_ids = [0] * len(ids)


class _StubTokenizer:
    """Mimics the ``tokenizers.Tokenizer`` surface used by ``_tokenize_onnx``."""

    def __init__(self, encodings: list[_StubEncoding]) -> None:
        self._encodings = encodings
        self.truncation_max_length: int | None = None

    def enable_truncation(self, max_length: int) -> None:
        self.truncation_max_length = max_length

    def enable_padding(self) -> None:
        pass

    def encode_batch(self, texts):
        return self._encodings


class _StubSession:
    """Mimics an onnxruntime session: returns an ndim-3 array so pooling
    over ``last_hidden_state`` is exercised. Token i's hidden vector is the
    constant ``i + 1`` so mean-pooling differs observably from CLS
    (first-token) pooling."""

    def __init__(self, dims: int = 4) -> None:
        self._dims = dims

    def get_inputs(self):
        class _Input:
            def __init__(self, name: str) -> None:
                self.name = name

        return [_Input("input_ids"), _Input("attention_mask"), _Input("token_type_ids")]

    def run(self, _output_names, feed):
        import numpy as np

        ids = feed["input_ids"]
        batch, seq_len = ids.shape
        row = [[float(i + 1)] * self._dims for i in range(seq_len)]
        out = np.asarray([row for _ in range(batch)], dtype=np.float32)
        return [out]


def _make_provider(model_name: str) -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider(model_name, onnx_dir=Path("/nonexistent"))


def test_mean_pooling_used_for_gemma():
    provider = _make_provider("google/embeddinggemma-300m")
    session = _StubSession(dims=4)
    # Two tokens attended, one padding position masked out.
    encoding = _StubEncoding(ids=[10, 11, 0], attention_mask=[1, 1, 0])
    provider._onnx_session = session
    provider._onnx_tokenizer = _StubTokenizer([encoding])
    provider._onnx_input_names = ["input_ids", "attention_mask", "token_type_ids"]
    provider._dim = 4

    out = provider._encode_onnx(["hi"], batch_size=32)

    # Token vectors are [1,1,1,1] and [2,2,2,2] (3rd token masked out).
    # Masked mean = (1+2)/2 = 1.5, NOT the CLS (first-token) value of 1.0.
    assert out.shape == (1, 4)
    assert out[0].tolist() == [1.5, 1.5, 1.5, 1.5]


def test_cls_pooling_kept_for_bge():
    provider = _make_provider("BAAI/bge-small-zh-v1.5")
    session = _StubSession(dims=4)
    encoding = _StubEncoding(ids=[10, 11, 0], attention_mask=[1, 1, 0])
    provider._onnx_session = session
    provider._onnx_tokenizer = _StubTokenizer([encoding])
    provider._onnx_input_names = ["input_ids", "attention_mask", "token_type_ids"]
    provider._dim = 4

    out = provider._encode_onnx(["hi"], batch_size=32)

    # CLS pooling takes the first token's vector: [1,1,1,1].
    assert out.shape == (1, 4)
    assert out[0].tolist() == [1.0, 1.0, 1.0, 1.0]


def test_resolution_prefers_bundled_then_downloaded(tmp_path, monkeypatch):
    downloaded_dir = tmp_path / "downloaded"
    downloaded_dir.mkdir()

    monkeypatch.setattr(
        LocalEmbeddingProvider,
        "_BUNDLED_ONNX_DIR",
        tmp_path / "no-such-bge-dir",
    )
    monkeypatch.setattr(
        "agentos.memory.model_download.downloaded_model_dir",
        lambda model_name: downloaded_dir,
    )

    resolved = LocalEmbeddingProvider.resolve_onnx_dir("google/embeddinggemma-300m")

    assert resolved == downloaded_dir


def test_resolution_prefers_bundled_when_present(tmp_path, monkeypatch):
    bundled_dir = tmp_path / "embeddings" / "embeddinggemma-300m-int8"
    bundled_dir.mkdir(parents=True)

    monkeypatch.setattr(
        LocalEmbeddingProvider,
        "_BUNDLED_ONNX_DIR",
        tmp_path / "bge_onnx",
    )
    monkeypatch.setattr(
        "agentos.memory.model_download.downloaded_model_dir",
        lambda model_name: (_ for _ in ()).throw(
            AssertionError("should not consult downloaded_model_dir when bundled hits")
        ),
    )

    resolved = LocalEmbeddingProvider.resolve_onnx_dir("google/embeddinggemma-300m")

    assert resolved == bundled_dir


def test_embed_query_applies_gemma_prefix(monkeypatch):
    import asyncio

    provider = _make_provider("google/embeddinggemma-300m")
    seen: list[list[str]] = []

    def fake_encode_sync(texts, *, batch_size=32, show_progress_bar=False, role=None):
        seen.append(list(texts))

        class _Arr(list):
            def tolist(self):
                return self

        return [_Arr([0.0])]

    monkeypatch.setattr(provider, "encode_sync", fake_encode_sync)
    asyncio.run(provider.embed_query("hello"))

    assert seen == [["task: search result | query: hello"]]


def test_embed_batch_applies_gemma_document_prefix(monkeypatch):
    import asyncio

    provider = _make_provider("google/embeddinggemma-300m")
    seen: list[list[str]] = []

    def fake_encode_sync(texts, *, batch_size=32, show_progress_bar=False, role=None):
        seen.append(list(texts))

        class _Arr(list):
            def tolist(self):
                return self

        return [_Arr([0.0]), _Arr([0.0])]

    monkeypatch.setattr(provider, "encode_sync", fake_encode_sync)
    asyncio.run(provider.embed_batch(["d1", "d2"]))

    assert seen == [["title: none | text: d1", "title: none | text: d2"]]


def test_embed_query_no_prefix_for_bge(monkeypatch):
    import asyncio

    provider = _make_provider("BAAI/bge-small-zh-v1.5")
    seen: list[list[str]] = []

    def fake_encode_sync(texts, *, batch_size=32, show_progress_bar=False, role=None):
        seen.append(list(texts))

        class _Arr(list):
            def tolist(self):
                return self

        return [_Arr([0.0])]

    monkeypatch.setattr(provider, "encode_sync", fake_encode_sync)
    asyncio.run(provider.embed_query("hello"))

    assert seen == [["hello"]]
