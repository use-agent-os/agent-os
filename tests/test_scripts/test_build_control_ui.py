from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_control_ui.py"
REPO_ROOT = SCRIPT_PATH.parents[1]
DIST_REL = Path("src/agentos/gateway/static/dist")


def load_script():
    spec = importlib.util.spec_from_file_location("build_control_ui", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_frontend_fixture(root: Path) -> tuple[Path, bytes]:
    frontend = root / "frontend"
    package_dir = frontend / "node_modules" / "runtime-package"
    package_dir.mkdir(parents=True)
    font_dir = frontend / "src" / "assets" / "fonts"
    font_dir.mkdir(parents=True)
    (font_dir / "Inter-LICENSE.txt").write_text(
        "Inter font license fixture.\n",
        encoding="utf-8",
    )
    (font_dir / "JetBrainsMono-LICENSE.txt").write_text(
        "JetBrains Mono font license fixture.\n",
        encoding="utf-8",
    )
    license_bytes = b"Runtime license, byte-for-byte.\nSecond line.\n"
    (package_dir / "LICENSE").write_bytes(license_bytes)
    (package_dir / "package.json").write_text(
        json.dumps({"name": "runtime-package", "version": "1.2.3"}),
        encoding="utf-8",
    )
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "name": "fixture",
                        "devDependencies": {"dev-package": "4.0.0"},
                    },
                    "node_modules/runtime-package": {
                        "version": "1.2.3",
                        "license": "MIT",
                    },
                    "node_modules/@types/runtime-package": {
                        "version": "1.0.0",
                        "license": "MIT",
                    },
                    "node_modules/csstype": {
                        "version": "3.0.0",
                        "license": "MIT",
                    },
                    "node_modules/dev-package": {
                        "version": "4.0.0",
                        "license": "ISC",
                        "dev": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return frontend, license_bytes


def write_dist_fixture(root: Path) -> Path:
    dist = root / DIST_REL
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "app-a1b2c3.js").write_text("console.log('ready')\n", encoding="utf-8")
    (assets / "app-a1b2c3.css").write_text(":root { color: lime; }\n", encoding="utf-8")
    (dist / "theme-bootstrap.js").write_text(
        "document.documentElement.dataset.ready = '1'\n",
        encoding="utf-8",
    )
    (dist / "index.html").write_text(
        "\n".join(
            (
                "<!doctype html>",
                '<base data-agentos-control-base href="./">',
                '<script src="./theme-bootstrap.js"></script>',
                '<script type="module" src="./assets/app-a1b2c3.js"></script>',
                '<link rel="stylesheet" href="./assets/app-a1b2c3.css">',
            )
        ),
        encoding="utf-8",
    )
    return dist


def test_license_bundle_is_deterministic_verbatim_and_runtime_only(tmp_path: Path) -> None:
    module = load_script()
    frontend, license_bytes = write_frontend_fixture(tmp_path)

    first = module.render_third_party_licenses(frontend)
    second = module.render_third_party_licenses(frontend)

    assert first == second
    assert license_bytes in first
    assert b"Package: runtime-package@1.2.3\n" in first
    assert b"Declared license: MIT\n" in first
    assert b"@types/runtime-package" not in first
    assert b"csstype" not in first
    assert b"dev-package" not in first
    assert b"Inter font license fixture." in first
    assert b"JetBrains Mono font license fixture." in first


def test_verify_requires_license_bundle_and_runtime_relative_assets(tmp_path: Path) -> None:
    module = load_script()
    frontend, _ = write_frontend_fixture(tmp_path)
    dist = write_dist_fixture(tmp_path)

    with pytest.raises(module.ControlUIError, match="license bundle is missing"):
        module.verify_control_ui(tmp_path)

    module.generate_third_party_licenses(frontend, dist)
    module.verify_control_ui(tmp_path)


@pytest.mark.parametrize(
    "base_tags",
    (
        "",
        (
            '<base data-agentos-control-base href="./">'
            '<base data-agentos-control-base href="./">'
        ),
    ),
)
def test_verify_requires_exactly_one_runtime_base_marker(
    tmp_path: Path,
    base_tags: str,
) -> None:
    module = load_script()
    frontend, _ = write_frontend_fixture(tmp_path)
    dist = write_dist_fixture(tmp_path)
    module.generate_third_party_licenses(frontend, dist)
    (dist / "index.html").write_text(
        (
            f"{base_tags}"
            '<script type="module" src="./assets/app-a1b2c3.js"></script>'
            '<link rel="stylesheet" href="./assets/app-a1b2c3.css">'
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.ControlUIError, match="exactly one"):
        module.verify_control_ui(tmp_path)


@pytest.mark.parametrize(
    ("generated_text", "message"),
    (
        ("fetch('/control/api/bootstrap')", "hard-codes /control"),
        ("const old = 'static/js/app.js'", "legacy reference"),
        ("const old = 'templates/index.html'", "legacy reference"),
    ),
)
def test_verify_rejects_non_portable_or_legacy_references(
    tmp_path: Path,
    generated_text: str,
    message: str,
) -> None:
    module = load_script()
    frontend, _ = write_frontend_fixture(tmp_path)
    dist = write_dist_fixture(tmp_path)
    module.generate_third_party_licenses(frontend, dist)
    (dist / "assets" / "app-a1b2c3.js").write_text(generated_text, encoding="utf-8")

    with pytest.raises(module.ControlUIError, match=message):
        module.verify_control_ui(tmp_path)


def test_node_22_or_newer_is_required(monkeypatch) -> None:
    module = load_script()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="v21.7.0\n"),
    )

    with pytest.raises(module.ControlUIError, match=r"Node\.js 22\+ is required"):
        module.require_supported_node()


def test_build_runs_clean_install_build_license_generation_then_verify(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = load_script()
    calls: list[object] = []
    monkeypatch.setattr(module, "require_supported_node", lambda: calls.append("node"))
    monkeypatch.setattr(
        module,
        "_run_npm",
        lambda args, frontend_dir: calls.append((tuple(args), frontend_dir)),
    )
    monkeypatch.setattr(
        module,
        "generate_third_party_licenses",
        lambda frontend_dir, dist_dir: calls.append(("licenses", frontend_dir, dist_dir)),
    )
    monkeypatch.setattr(
        module,
        "verify_control_ui",
        lambda repo_root: calls.append(("verify", repo_root)),
    )

    module.build_control_ui(tmp_path)

    frontend = tmp_path / "frontend"
    dist = tmp_path / DIST_REL
    assert calls == [
        "node",
        (("npm", "ci"), frontend),
        (("npm", "run", "build"), frontend),
        ("licenses", frontend, dist),
        ("verify", tmp_path),
    ]


def test_npm_failure_is_reported_as_control_ui_error(monkeypatch, tmp_path: Path) -> None:
    module = load_script()

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(2, ["npm", "ci"])

    monkeypatch.setattr(module.subprocess, "run", fail)

    with pytest.raises(module.ControlUIError, match="npm ci"):
        module._run_npm(["npm", "ci"], tmp_path)


def test_run_npm_resolves_windows_cmd_shim(monkeypatch, tmp_path: Path) -> None:
    module = load_script()
    npm_cmd = r"C:\hostedtoolcache\windows\node\22.23.1\x64\npm.cmd"
    resolved_candidates: list[str] = []
    calls: list[list[str]] = []

    def resolve(candidate: str) -> str | None:
        resolved_candidates.append(candidate)
        return npm_cmd if candidate == "npm.cmd" else None

    def run(args, **kwargs) -> None:
        calls.append(args)

    monkeypatch.setattr(module, "IS_WINDOWS", True)
    monkeypatch.setattr(module.shutil, "which", resolve)
    monkeypatch.setattr(module.subprocess, "run", run)

    module._run_npm(["npm", "ci"], tmp_path)

    assert resolved_candidates == ["npm.cmd"]
    assert calls == [[npm_cmd, "ci"]]


def test_hatch_includes_ignored_control_ui_and_excludes_local_sdist_trees() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = pyproject["tool"]["hatch"]["build"]["targets"]
    artifact = "src/agentos/gateway/static/dist/**"

    assert artifact in targets["wheel"]["artifacts"]
    assert artifact in targets["sdist"]["artifacts"]
    assert "/frontend/node_modules/**" in targets["sdist"]["exclude"]
    assert "/dist/**" in targets["sdist"]["exclude"]
    assert "/build/**" in targets["sdist"]["exclude"]


def _write_packaging_fixture(project: Path, module) -> None:
    for filename in (
        "LICENSE",
        "NOTICE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
    ):
        if filename == "pyproject.toml":
            shutil.copy2(REPO_ROOT / filename, project / filename)
        else:
            (project / filename).write_text(f"{filename} fixture\n", encoding="utf-8")

    (project / "src" / "agentos").mkdir(parents=True)
    (project / "src" / "agentos" / "__init__.py").write_text("", encoding="utf-8")
    dist = write_dist_fixture(project)
    (dist / module.LICENSE_BUNDLE_NAME).write_bytes(
        (module.LICENSE_BUNDLE_PREAMBLE + "\nPackage: fixture@1.0.0\n").encode()
    )

    references = (
        project / "src" / "agentos" / "skills" / "bundled" / "pptx" / "references"
    )
    references.mkdir(parents=True)
    (references / "pptxgenjs.md").write_text("fixture\n", encoding="utf-8")
    (references / "python_pptx.md").write_text("fixture\n", encoding="utf-8")
    (project / "migrations").mkdir()
    (project / "migrations" / "001_fixture.py").write_text("", encoding="utf-8")

    frontend = project / "frontend"
    (frontend / "node_modules" / "leaked-package").mkdir(parents=True)
    (frontend / "package.json").write_text("{}\n", encoding="utf-8")
    (frontend / "node_modules" / "leaked-package" / "secret.js").write_text(
        "must not ship\n",
        encoding="utf-8",
    )
    (project / "arbitrary-local-scratch.txt").write_text("must not ship\n", encoding="utf-8")


def test_sdist_allowlist_and_wheel_from_sdist_preserve_control_ui(tmp_path: Path) -> None:
    module = load_script()
    uv = shutil.which("uv")
    assert uv is not None, "The repository test contract requires uv"
    project = tmp_path / "project"
    project.mkdir()
    _write_packaging_fixture(project, module)
    sdist_dir = tmp_path / "sdist"

    subprocess.run(
        [uv, "build", "--sdist", "--offline", "--out-dir", str(sdist_dir), str(project)],
        check=True,
    )
    sdist_path = next(sdist_dir.glob("*.tar.gz"))
    with tarfile.open(sdist_path, "r:gz") as archive:
        names = archive.getnames()
        assert any(name.endswith("/src/agentos/gateway/static/dist/index.html") for name in names)
        assert not any("node_modules" in name.split("/") for name in names)
        assert not any(name.endswith("/arbitrary-local-scratch.txt") for name in names)
        archive.extractall(tmp_path / "extracted", filter="data")

    extracted_project = next((tmp_path / "extracted").iterdir())
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        [
            uv,
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(wheel_dir),
            str(extracted_project),
        ],
        check=True,
    )
    wheel_path = next(wheel_dir.glob("*.whl"))

    module.verify_archive(sdist_path)
    module.verify_archive(wheel_path)
