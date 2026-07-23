"""Keep the public browser-bundle notices synchronized with npm's lockfile."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "frontend" / "package-lock.json"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"

_ALLOWED_RUNTIME_LICENSES = {
    "0BSD",
    "Apache-2.0",
    "BSD-3-Clause",
    "ISC",
    "MIT",
    "(MPL-2.0 OR Apache-2.0)",
}


def _production_packages() -> list[tuple[str, str, str]]:
    lock: dict[str, Any] = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    packages: list[tuple[str, str, str]] = []
    for package_path, metadata in lock["packages"].items():
        if not package_path or metadata.get("dev"):
            continue
        name = package_path.rsplit("node_modules/", maxsplit=1)[-1]
        if name.startswith("@types/") or name == "csstype":
            continue
        packages.append((name, metadata["version"], metadata["license"]))
    return sorted(packages)


def test_frontend_runtime_licenses_are_reviewed_and_attributed() -> None:
    notices = NOTICES.read_text(encoding="utf-8")
    packages = _production_packages()

    assert packages
    assert {license_name for _, _, license_name in packages} <= _ALLOWED_RUNTIME_LICENSES
    for name, version, _license_name in packages:
        assert f"`{name}@{version}`" in notices


def test_frontend_notices_describe_the_generated_exact_license_bundle() -> None:
    notices = NOTICES.read_text(encoding="utf-8")

    assert "static/dist/THIRD_PARTY_LICENSES.txt" in notices
    assert "## Vendored Web UI JavaScript libraries" not in notices
    assert "gateway/static/vendor/" not in notices
    assert "gateway/templates/index.html" not in notices


def test_bundled_font_binaries_keep_their_ofl_notices() -> None:
    notices = NOTICES.read_text(encoding="utf-8")
    font_dir = ROOT / "frontend" / "src" / "assets" / "fonts"

    expected = {
        "Inter-Variable.woff2": (
            "Inter-LICENSE.txt",
            "Copyright (c) 2016 The Inter Project Authors",
        ),
        "JetBrainsMono-Variable.woff2": (
            "JetBrainsMono-LICENSE.txt",
            "Copyright 2020 The JetBrains Mono Project Authors",
        ),
    }
    for font_name, (license_name, copyright_line) in expected.items():
        assert (font_dir / font_name).is_file()
        license_text = (font_dir / license_name).read_text(encoding="utf-8")
        assert copyright_line in license_text
        assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
        assert font_name.removesuffix("-Variable.woff2") in notices
        assert license_name in notices
