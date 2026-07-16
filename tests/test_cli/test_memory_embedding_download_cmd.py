"""CLI tests for `agentos memory embedding-download`."""

from __future__ import annotations

from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()


def test_embedding_download_reports_target_and_reindex_hint(tmp_path, monkeypatch) -> None:
    async def fake_download(model_id, *, progress=None):
        assert model_id == "google/embeddinggemma-300m"
        if progress is not None:
            progress("tokenizer.json", 10, 20)
        target = tmp_path / "embeddinggemma-300m-q8"
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(
        "agentos.memory.model_download.download_embedding_model", fake_download
    )

    result = runner.invoke(app, ["memory", "embedding-download"])

    assert result.exit_code == 0, result.stdout
    assert "embeddinggemma-300m-q8" in result.stdout
    assert "tokenizer.json" in result.stdout
    normalized = " ".join(result.stdout.split())
    assert (
        "Restart the gateway (or run `agentos memory index --force`) to reindex "
        "with the new model." in normalized
    )


def test_embedding_download_accepts_model_option(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_download(model_id, *, progress=None):
        calls.append(model_id)
        return tmp_path / "embeddinggemma-300m-q8"

    monkeypatch.setattr(
        "agentos.memory.model_download.download_embedding_model", fake_download
    )

    result = runner.invoke(
        app, ["memory", "embedding-download", "--model", "google/embeddinggemma-300m"]
    )

    assert result.exit_code == 0, result.stdout
    assert calls == ["google/embeddinggemma-300m"]


def test_embedding_download_unknown_model_exits_nonzero() -> None:
    result = runner.invoke(app, ["memory", "embedding-download", "--model", "nope/none"])

    assert result.exit_code == 1
    assert "Unknown embedding model id" in result.stdout
