"""Contract tests for the Pilot fixture model + ``PilotModel`` loader (T2).

These run against the *real* committed fixture artifacts under
``data/pilot_fixture/`` (``model.onnx`` + ``manifest.json``) produced by
``scripts/pilot_router/make_fixture_model.py``. They pin the production ONNX
IO contract, the manifest schema, the fail-soft (unavailable) semantics, and
the log-space temperature calibration.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from agentos.agentos_router.pilot.features import FEATURE_DIM, SCALAR_FEATURE_NAMES
from agentos.agentos_router.pilot.model import PilotModel

FIXTURE_DIR = Path(__file__).parent / "data" / "pilot_fixture"


def _copy_fixture(dst: Path) -> Path:
    shutil.copytree(FIXTURE_DIR, dst)
    return dst


@pytest.fixture
def model() -> PilotModel:
    return PilotModel(FIXTURE_DIR)


# --- Load + happy path ------------------------------------------------------


def test_fixture_loads_available(model: PilotModel) -> None:
    assert model.available is True
    assert model.unavailable_reason is None
    assert model.classes == ["R0", "R1", "R2", "R3"]


def test_predict_proba_shape_and_normalization(model: PilotModel) -> None:
    x = np.zeros((3, FEATURE_DIM), dtype=np.float32)
    probs = model.predict_proba(x)
    assert probs.shape == (3, 4)
    assert probs.dtype == np.float32
    row_sums = probs.sum(axis=1)
    np.testing.assert_allclose(row_sums, np.ones(3), atol=1e-5)
    assert np.all(probs >= 0.0)


def test_predict_proba_single_row(model: PilotModel) -> None:
    x = np.zeros((1, FEATURE_DIM), dtype=np.float32)
    probs = model.predict_proba(x)
    assert probs.shape == (1, 4)
    np.testing.assert_allclose(probs.sum(), 1.0, atol=1e-5)


def test_manifest_exposed(model: PilotModel) -> None:
    assert model.manifest["classes"] == ["R0", "R1", "R2", "R3"]
    assert model.manifest["feature_schema"]["scalar_names"] == list(SCALAR_FEATURE_NAMES)
    assert model.manifest["feature_schema"]["feature_dim"] == FEATURE_DIM


def test_manifest_pins_reference_regexes(model: PilotModel) -> None:
    """Spec §4.4: the URL/FILE reference regexes are pinned in the
    feature-schema manifest — the train/serve tripwire for regex edits."""
    from agentos.agentos_router.pilot.features import FILE_RE, URL_RE

    schema = model.manifest["feature_schema"]
    assert schema["url_regex"] == URL_RE.pattern
    assert schema["file_regex"] == FILE_RE.pattern


# --- Round-trip via the generator ------------------------------------------


def test_generator_round_trip(tmp_path: Path) -> None:
    # The generator needs the dev-only ``pilot-train`` dependency group
    # (scikit-learn + skl2onnx); CI installs only the runtime extras.
    pytest.importorskip("skl2onnx")
    pytest.importorskip("sklearn")
    from scripts.pilot_router.make_fixture_model import build_fixture

    out = tmp_path / "gen"
    build_fixture(out)
    m = PilotModel(out)
    assert m.available is True
    probs = m.predict_proba(np.zeros((2, FEATURE_DIM), dtype=np.float32))
    assert probs.shape == (2, 4)
    np.testing.assert_allclose(probs.sum(axis=1), np.ones(2), atol=1e-5)


# --- Fail-soft: unavailable, never raises ----------------------------------


def test_missing_dir_unavailable(tmp_path: Path) -> None:
    m = PilotModel(tmp_path / "does_not_exist")
    assert m.available is False
    assert m.unavailable_reason


def test_predict_on_unavailable_returns_uniform_never_raises(tmp_path: Path) -> None:
    m = PilotModel(tmp_path / "does_not_exist")
    assert m.available is False
    probs = m.predict_proba(np.zeros((2, FEATURE_DIM), dtype=np.float32))
    assert probs.shape == (2, 4)
    np.testing.assert_allclose(probs, np.full((2, 4), 0.25), atol=1e-6)


def test_missing_manifest_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    (d / "manifest.json").unlink()
    m = PilotModel(d)
    assert m.available is False
    assert m.unavailable_reason


def test_missing_onnx_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    (d / "model.onnx").unlink()
    m = PilotModel(d)
    assert m.available is False
    assert m.unavailable_reason


def test_corrupt_manifest_json_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    (d / "manifest.json").write_text("{not valid json", encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False
    assert m.unavailable_reason


def test_corrupt_onnx_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    (d / "model.onnx").write_bytes(b"not a real onnx graph")
    m = PilotModel(d)
    assert m.available is False
    assert m.unavailable_reason


def test_sha256_mismatch_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["model_onnx_sha256"] = "0" * 64
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False
    assert "sha256" in m.unavailable_reason.lower()


def test_io_contract_wrong_input_shape_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["io_contract"]["input"]["shape"] = [None, 999]
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False


def test_io_contract_wrong_output_name_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["io_contract"]["outputs"][1]["name"] = "not_probabilities"
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False


def test_class_order_mismatch_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["classes"] = ["R3", "R2", "R1", "R0"]
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False


# --- Manifest schema validation --------------------------------------------


@pytest.mark.parametrize(
    "drop",
    [
        "pilot_version",
        "classes",
        "temperature",
        "embedder_id",
        "model_onnx_sha256",
        "io_contract",
        "encoder_contract",
        "feature_schema",
    ],
)
def test_missing_required_field_unavailable(tmp_path: Path, drop: str) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    del manifest[drop]
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False


def test_invalid_temperature_type_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["temperature"] = "hot"
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False


def test_feature_schema_scalar_mismatch_unavailable(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["feature_schema"]["scalar_names"] = ["wrong"]
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False


@pytest.mark.parametrize("key", ["url_regex", "file_regex"])
def test_feature_schema_regex_mismatch_unavailable(tmp_path: Path, key: str) -> None:
    """An edited reference regex (train/serve skew) must fail the manifest pin."""
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["feature_schema"][key] = r"(?i)\bsomething-else\b"
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False
    assert key in (m.unavailable_reason or "")


@pytest.mark.parametrize("key", ["url_regex", "file_regex"])
def test_feature_schema_regex_missing_unavailable(tmp_path: Path, key: str) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    del manifest["feature_schema"][key]
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    m = PilotModel(d)
    assert m.available is False


# --- Temperature calibration (log-space) -----------------------------------


def _reference_calibrate(p: np.ndarray, temperature: float) -> np.ndarray:
    """Reference: q = softmax(log(clip(p, 1e-7, 1)) / T), row-wise."""
    logits = np.log(np.clip(p, 1e-7, 1.0)) / temperature
    logits = logits - logits.max(axis=1, keepdims=True)
    ex = np.exp(logits)
    return (ex / ex.sum(axis=1, keepdims=True)).astype(np.float32)


def test_temperature_one_equals_graph_probs(model: PilotModel) -> None:
    # T=1.0: calibrated output equals the raw graph probabilities.
    rng = np.random.default_rng(0)
    x = rng.standard_normal((5, FEATURE_DIM)).astype(np.float32)
    graph_probs = model.raw_proba(x)
    calibrated = model.predict_proba(x)
    np.testing.assert_allclose(calibrated, graph_probs, atol=1e-5)


def test_temperature_not_one_matches_reference(tmp_path: Path) -> None:
    d = _copy_fixture(tmp_path / "fx")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["temperature"] = 2.0
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # sha256 unchanged, model.onnx unchanged → still loadable.
    m = PilotModel(d)
    assert m.available is True

    rng = np.random.default_rng(1)
    x = rng.standard_normal((6, FEATURE_DIM)).astype(np.float32)
    graph_probs = m.raw_proba(x)
    expected = _reference_calibrate(graph_probs, 2.0)
    got = m.predict_proba(x)
    np.testing.assert_allclose(got, expected, atol=1e-6)
    np.testing.assert_allclose(got.sum(axis=1), np.ones(6), atol=1e-5)
    # T>1 flattens the distribution: max prob should not exceed the raw max.
    assert got.max(axis=1).mean() <= graph_probs.max(axis=1).mean() + 1e-6
