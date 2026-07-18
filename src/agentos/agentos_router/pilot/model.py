"""Pilot router model loader — ``PilotModel`` (T2).

Loads and validates the fixture/production artifact pair (``model.onnx`` +
``manifest.json``) from an artifact directory, then exposes calibrated
per-class probabilities.

The design mirrors ``V4Phase3Strategy``'s **fail-soft** contract: a missing
directory, a missing/corrupt file, a sha256 or IO-contract mismatch, or a
class-order mismatch all leave the model in an **unavailable** state (a
queryable ``available`` flag + ``unavailable_reason``) rather than raising.
Inference errors at predict time degrade the same way — ``predict_proba``
never raises for artifact/runtime faults.

Validation performed at load:

* manifest JSON parses and carries every required field with the right types;
* ``sha256(model.onnx)`` matches ``manifest["model_onnx_sha256"]``;
* the ONNX session's input/output names, dtypes, and shapes match the
  manifest ``io_contract``;
* ``manifest["classes"]`` is the pinned order ``["R0", "R1", "R2", "R3"]`` and
  matches the probability-tensor width;
* ``feature_schema.scalar_names`` matches ``pilot.features.SCALAR_FEATURE_NAMES``;
* ``feature_schema.url_regex`` / ``file_regex`` match the pinned
  ``pilot.features`` reference patterns (``URL_RE`` / ``FILE_RE``).

Probability post-processing applies **log-space temperature calibration**
(part of classification, not policy)::

    q = softmax(log(clip(p, 1e-7, 1)) / T)

with ``T`` from the manifest. The graph already emits probabilities ``p`` (no
ZipMap); we never apply a second softmax to logits.

Inference deps: ``onnxruntime`` + ``numpy`` only — the same extras the v4
router uses; they are imported lazily so importing this module stays cheap.
The ONNX session is single-threaded, CPU-only, matching the repo convention.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from agentos.agentos_router.pilot.features import (
    FEATURE_DIM,
    FILE_RE,
    SCALAR_FEATURE_NAMES,
    URL_RE,
)

log = structlog.get_logger(__name__)

#: Pinned class order (Pilot spec, Rev 4). Index-aligned with probability cols.
PILOT_CLASSES: list[str] = ["R0", "R1", "R2", "R3"]

#: Lower clip for log-space calibration (avoids ``log(0)``).
_PROBA_CLIP_MIN = 1e-7

# ONNX-runtime element-type strings mapped to the manifest's dtype names.
_ORT_DTYPE_TO_NAME = {
    "tensor(float)": "float32",
    "tensor(int64)": "int64",
}


class _ManifestError(Exception):
    """Raised internally when the manifest is missing fields or malformed.

    Never escapes ``PilotModel`` — it is caught and turned into an
    ``unavailable`` state with a human-readable reason.
    """


class PilotModel:
    """Fail-soft loader for a Pilot artifact directory.

    Construction always succeeds. Inspect :attr:`available` /
    :attr:`unavailable_reason` before relying on predictions; when
    unavailable, :meth:`predict_proba` returns a uniform distribution rather
    than raising.
    """

    def __init__(self, artifact_dir: str | Path) -> None:
        self.artifact_dir = Path(artifact_dir)
        self._available = False
        self._reason: str | None = None
        self._manifest: dict[str, Any] = {}
        self._classes: list[str] = []
        self._temperature: float = 1.0
        self._session: Any = None
        self._input_name: str = "input"
        self._proba_output_name: str = "probabilities"

        try:
            self._load()
        except _ManifestError as exc:
            self._mark_unavailable(str(exc))
        except FileNotFoundError as exc:
            self._mark_unavailable(f"missing artifact: {exc}")
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            self._mark_unavailable(f"load failed: {exc}")

    # --- Public surface ----------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest

    @property
    def classes(self) -> list[str]:
        return list(self._classes)

    @property
    def temperature(self) -> float:
        return self._temperature

    def raw_proba(self, features: np.ndarray) -> np.ndarray:
        """Uncalibrated graph probabilities ``float32 [N, 4]`` for ``features``.

        Fail-soft: on an unavailable model or any runtime error, returns a
        uniform distribution and flips the model unavailable — never raises.
        """
        return self._run(features, calibrate=False)

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        """Calibrated per-class probabilities ``float32 [N, 4]`` for ``features``.

        Applies log-space temperature calibration with ``T`` from the
        manifest. Class order is the manifest's pinned order. Fail-soft: on an
        unavailable model or any runtime error, returns a uniform distribution
        and flips the model unavailable — never raises.
        """
        return self._run(features, calibrate=True)

    # --- Inference ---------------------------------------------------------

    def _run(self, features: np.ndarray, *, calibrate: bool) -> np.ndarray:
        arr = np.asarray(features)
        n = int(arr.shape[0]) if arr.ndim == 2 else 1
        if not self._available or self._session is None:
            return self._uniform(n)
        try:
            x = np.ascontiguousarray(features, dtype=np.float32)
            if x.ndim != 2 or x.shape[1] != FEATURE_DIM:
                raise ValueError(
                    f"expected features of shape [N, {FEATURE_DIM}], got {x.shape}"
                )
            n = x.shape[0]
            outputs = self._session.run([self._proba_output_name], {self._input_name: x})
            probs = np.asarray(outputs[0], dtype=np.float32)
            if calibrate:
                probs = self._calibrate(probs)
            return probs
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            self._mark_unavailable(f"predict failed: {exc}")
            return self._uniform(n)

    def _calibrate(self, probs: np.ndarray) -> np.ndarray:
        """``q = softmax(log(clip(p, 1e-7, 1)) / T)`` row-wise."""
        logits = np.log(np.clip(probs, _PROBA_CLIP_MIN, 1.0)) / self._temperature
        logits = logits - logits.max(axis=1, keepdims=True)
        ex = np.exp(logits)
        calibrated: np.ndarray = ex / ex.sum(axis=1, keepdims=True)
        return calibrated.astype(np.float32)

    def _uniform(self, n: int) -> np.ndarray:
        k = len(self._classes) or len(PILOT_CLASSES)
        return np.full((max(n, 0), k), 1.0 / k, dtype=np.float32)

    # --- Load + validate ---------------------------------------------------

    def _load(self) -> None:
        if not self.artifact_dir.is_dir():
            raise FileNotFoundError(f"artifact dir not found: {self.artifact_dir}")

        onnx_path = self.artifact_dir / "model.onnx"
        manifest_path = self.artifact_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"manifest.json not found in {self.artifact_dir}")
        if not onnx_path.is_file():
            raise FileNotFoundError(f"model.onnx not found in {self.artifact_dir}")

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise _ManifestError(f"manifest.json is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise _ManifestError("manifest.json must be a JSON object")

        self._validate_manifest_schema(manifest)

        model_bytes = onnx_path.read_bytes()
        self._validate_sha256(model_bytes, manifest["model_onnx_sha256"])

        session = self._make_session(model_bytes)
        self._validate_io_contract(session, manifest["io_contract"])

        # Everything validated — commit state.
        self._manifest = manifest
        self._classes = list(manifest["classes"])
        self._temperature = float(manifest["temperature"])
        self._input_name = manifest["io_contract"]["input"]["name"]
        self._proba_output_name = manifest["io_contract"]["outputs"][1]["name"]
        self._session = session
        self._available = True
        self._reason = None

    def _validate_manifest_schema(self, manifest: dict[str, Any]) -> None:
        required = (
            "pilot_version",
            "classes",
            "temperature",
            "embedder_id",
            "model_onnx_sha256",
            "io_contract",
            "encoder_contract",
            "feature_schema",
        )
        missing = [key for key in required if key not in manifest]
        if missing:
            raise _ManifestError(f"manifest missing required fields: {missing}")

        classes = manifest["classes"]
        if classes != PILOT_CLASSES:
            raise _ManifestError(
                f"class order mismatch: manifest {classes} != pinned {PILOT_CLASSES}"
            )

        temperature = manifest["temperature"]
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            raise _ManifestError(f"temperature must be a number, got {temperature!r}")
        if not float(temperature) > 0.0:
            raise _ManifestError(f"temperature must be positive, got {temperature!r}")

        if not isinstance(manifest["model_onnx_sha256"], str):
            raise _ManifestError("model_onnx_sha256 must be a string")

        self._validate_io_contract_schema(manifest["io_contract"])
        self._validate_feature_schema(manifest["feature_schema"])

    def _validate_io_contract_schema(self, io: Any) -> None:
        if not isinstance(io, dict) or "input" not in io or "outputs" not in io:
            raise _ManifestError("io_contract must have 'input' and 'outputs'")
        inp = io["input"]
        for key in ("name", "dtype", "shape"):
            if key not in inp:
                raise _ManifestError(f"io_contract.input missing '{key}'")
        outputs = io["outputs"]
        if not isinstance(outputs, list) or len(outputs) != 2:
            raise _ManifestError("io_contract.outputs must list exactly 2 outputs")
        for out in outputs:
            for key in ("name", "dtype", "shape"):
                if key not in out:
                    raise _ManifestError(f"io_contract output missing '{key}'")

    def _validate_feature_schema(self, schema: Any) -> None:
        if not isinstance(schema, dict):
            raise _ManifestError("feature_schema must be an object")
        names = schema.get("scalar_names")
        if names != list(SCALAR_FEATURE_NAMES):
            raise _ManifestError(
                "feature_schema.scalar_names does not match pilot.features "
                f"SCALAR_FEATURE_NAMES ({names!r})"
            )
        if schema.get("feature_dim") != FEATURE_DIM:
            raise _ManifestError(
                f"feature_schema.feature_dim must be {FEATURE_DIM}, "
                f"got {schema.get('feature_dim')!r}"
            )
        # Reference-regex pins (spec §6.5): the manifest must carry the exact
        # pattern strings the model was trained against; a drifted runtime
        # regex (or a stale manifest) is train/serve skew → unavailable.
        for key, compiled in (("url_regex", URL_RE), ("file_regex", FILE_RE)):
            if schema.get(key) != compiled.pattern:
                raise _ManifestError(
                    f"feature_schema.{key} does not match the pinned "
                    f"pilot.features pattern ({schema.get(key)!r})"
                )

    def _validate_sha256(self, model_bytes: bytes, expected: str) -> None:
        actual = hashlib.sha256(model_bytes).hexdigest()
        if actual != expected:
            raise _ManifestError(
                f"model.onnx sha256 mismatch: expected {expected}, got {actual}"
            )

    def _make_session(self, model_bytes: bytes) -> Any:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        return ort.InferenceSession(
            model_bytes,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

    def _validate_io_contract(self, session: Any, io: dict[str, Any]) -> None:
        # Input.
        inputs = session.get_inputs()
        if len(inputs) != 1:
            raise _ManifestError(f"expected 1 graph input, got {len(inputs)}")
        self._check_tensor(inputs[0], io["input"], role="input")

        # Outputs (order-pinned: label then probabilities).
        outputs = session.get_outputs()
        expected = io["outputs"]
        if len(outputs) != len(expected):
            raise _ManifestError(
                f"expected {len(expected)} graph outputs, got {len(outputs)}"
            )
        for got, want in zip(outputs, expected, strict=True):
            self._check_tensor(got, want, role="output")

    def _check_tensor(self, node: Any, spec: dict[str, Any], *, role: str) -> None:
        if node.name != spec["name"]:
            raise _ManifestError(
                f"{role} name mismatch: graph {node.name!r} != manifest {spec['name']!r}"
            )
        got_dtype = _ORT_DTYPE_TO_NAME.get(node.type, node.type)
        if got_dtype != spec["dtype"]:
            raise _ManifestError(
                f"{role} {node.name!r} dtype mismatch: graph {got_dtype} "
                f"!= manifest {spec['dtype']}"
            )
        self._check_shape(node.name, node.shape, spec["shape"], role=role)

    def _check_shape(
        self, name: str, got: list[Any], want: list[Any], *, role: str
    ) -> None:
        if len(got) != len(want):
            raise _ManifestError(
                f"{role} {name!r} rank mismatch: graph {got} != manifest {want}"
            )
        for g, w in zip(got, want, strict=True):
            if w is None:
                continue  # dynamic axis — any symbolic/int value is acceptable
            # Concrete manifest dim must match a concrete graph dim.
            if not isinstance(g, int) or g != w:
                raise _ManifestError(
                    f"{role} {name!r} shape mismatch: graph {got} != manifest {want}"
                )

    def _mark_unavailable(self, reason: str) -> None:
        self._available = False
        self._reason = reason
        self._session = None
        log.warning("pilot_model.unavailable", artifact_dir=str(self.artifact_dir), reason=reason)
