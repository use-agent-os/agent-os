#!/usr/bin/env python3
"""Build and verify the packaged React Control UI.

The build output is intentionally ignored by Git, so release and source-install
paths use this script as the single fail-closed contract for producing it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MINIMUM_NODE_MAJOR = 22
IS_WINDOWS = os.name == "nt"
LICENSE_BUNDLE_NAME = "THIRD_PARTY_LICENSES.txt"
LICENSE_BUNDLE_PREAMBLE = (
    "AgentOS Control UI third-party licenses\n"
    "Generated from frontend/package-lock.json and bundled asset licenses; "
    "do not edit by hand.\n"
)
BUNDLED_FONT_LICENSES = (
    "Inter-LICENSE.txt",
    "JetBrainsMono-LICENSE.txt",
)
REQUIRED_DIST_FILES = ("theme-bootstrap.js",)
TYPE_ONLY_PACKAGES = frozenset({"csstype"})
RUNTIME_TEXT_SUFFIXES = frozenset({".css", ".html", ".js", ".json", ".map", ".mjs"})
LEGACY_REFERENCES = (
    "static/css/",
    "static/fonts/",
    "static/img/",
    "static/js/",
    "static/vendor/",
    "templates/index.html",
)
ASSET_REFERENCE_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""")
BASE_MARKER_RE = re.compile(
    r"<base\b(?=[^>]*\bdata-agentos-control-base\b)[^>]*>",
    flags=re.IGNORECASE,
)
NODE_VERSION_RE = re.compile(r"^v?(?P<major>\d+)(?:\.\d+){1,2}(?:[-+].*)?$")
WHEEL_DIST_INDEX = "agentos/gateway/static/dist/index.html"
SDIST_DIST_INDEX_SUFFIX = "/src/agentos/gateway/static/dist/index.html"
FORBIDDEN_ARCHIVE_PATH_PARTS = (
    "/agentos/gateway/static/css/",
    "/agentos/gateway/static/fonts/",
    "/agentos/gateway/static/img/",
    "/agentos/gateway/static/js/",
    "/agentos/gateway/static/vendor/",
    "/agentos/gateway/templates/index.html",
)


class ControlUIError(RuntimeError):
    """Raised when the Control UI build contract is not satisfied."""


@dataclass(frozen=True, order=True)
class ProductionPackage:
    """One runtime npm package selected from package-lock.json."""

    name: str
    version: str
    license_expression: str
    lock_path: str

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.version}"


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ControlUIError(f"Required file is missing: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlUIError(f"Could not read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlUIError(f"Expected a JSON object in {path}")
    return value


def production_packages(lock_path: Path) -> tuple[ProductionPackage, ...]:
    """Return the unique, sorted production package set from an npm v3 lockfile."""

    lock = _read_json_object(lock_path)
    package_records = lock.get("packages")
    if not isinstance(package_records, dict):
        raise ControlUIError(f"Lockfile has no packages object: {lock_path}")

    packages: dict[tuple[str, str], ProductionPackage] = {}
    for raw_path, raw_metadata in package_records.items():
        if not isinstance(raw_path, str) or not raw_path.startswith("node_modules/"):
            continue
        if not isinstance(raw_metadata, dict):
            raise ControlUIError(f"Invalid package record for {raw_path!r} in {lock_path}")
        if raw_metadata.get("dev") is True:
            continue

        name = raw_path.rsplit("node_modules/", maxsplit=1)[-1]
        if name.startswith("@types/") or name in TYPE_ONLY_PACKAGES:
            continue

        version = raw_metadata.get("version")
        license_expression = raw_metadata.get("license")
        if not isinstance(version, str) or not version:
            raise ControlUIError(f"Production package {name!r} has no version in {lock_path}")
        if not isinstance(license_expression, str) or not license_expression:
            raise ControlUIError(f"Production package {name!r} has no license in {lock_path}")

        package = ProductionPackage(name, version, license_expression, raw_path)
        key = (name, version)
        existing = packages.get(key)
        if existing is not None:
            if existing.license_expression != license_expression:
                raise ControlUIError(
                    f"Conflicting licenses for {package.identifier}: "
                    f"{existing.license_expression!r} and {license_expression!r}"
                )
            if existing.lock_path <= raw_path:
                continue
        packages[key] = package

    if not packages:
        raise ControlUIError(f"No production packages found in {lock_path}")
    return tuple(sorted(packages.values()))


def _package_directory(frontend_dir: Path, package: ProductionPackage) -> Path:
    package_dir = (frontend_dir / package.lock_path).resolve()
    frontend_root = frontend_dir.resolve()
    if frontend_root not in package_dir.parents:
        raise ControlUIError(f"Unsafe package path in lockfile: {package.lock_path}")
    if not package_dir.is_dir():
        raise ControlUIError(
            f"Installed package is missing for {package.identifier}: {package_dir}. "
            "Run npm ci before generating licenses."
        )

    manifest = _read_json_object(package_dir / "package.json")
    installed_name = manifest.get("name")
    installed_version = manifest.get("version")
    if installed_name != package.name or installed_version != package.version:
        raise ControlUIError(
            f"Installed package does not match lockfile for {package.identifier}: "
            f"found {installed_name}@{installed_version}"
        )
    return package_dir


def _license_files(package_dir: Path) -> tuple[Path, ...]:
    prefixes = ("copying", "licence", "license")
    candidates = tuple(
        sorted(
            (
                path
                for path in package_dir.iterdir()
                if path.is_file() and path.name.lower().startswith(prefixes)
            ),
            key=lambda path: path.name.casefold(),
        )
    )
    if not candidates:
        raise ControlUIError(f"No upstream LICENSE file found in {package_dir}")
    return candidates


def render_third_party_licenses(frontend_dir: Path) -> bytes:
    """Render a deterministic bundle containing every upstream license verbatim."""

    packages = production_packages(frontend_dir / "package-lock.json")
    output = bytearray(LICENSE_BUNDLE_PREAMBLE.encode())
    for package in packages:
        package_dir = _package_directory(frontend_dir, package)
        output.extend(b"\n" + b"=" * 80 + b"\n")
        output.extend(f"Package: {package.identifier}\n".encode())
        output.extend(f"Declared license: {package.license_expression}\n".encode())
        for license_path in _license_files(package_dir):
            output.extend(f"License file: {license_path.name}\n".encode())
            output.extend(b"-" * 80 + b"\n")
            license_bytes = license_path.read_bytes()
            if not license_bytes:
                raise ControlUIError(f"Upstream license file is empty: {license_path}")
            output.extend(license_bytes)
            if not license_bytes.endswith(b"\n"):
                output.extend(b"\n")

    font_license_dir = frontend_dir / "src" / "assets" / "fonts"
    for filename in BUNDLED_FONT_LICENSES:
        license_path = font_license_dir / filename
        try:
            license_bytes = license_path.read_bytes()
        except OSError as exc:
            raise ControlUIError(f"Bundled font license is missing: {license_path}") from exc
        if not license_bytes:
            raise ControlUIError(f"Bundled font license is empty: {license_path}")
        output.extend(b"\n" + b"=" * 80 + b"\n")
        output.extend(f"Bundled font license: {filename}\n".encode())
        output.extend(b"-" * 80 + b"\n")
        output.extend(license_bytes)
        if not license_bytes.endswith(b"\n"):
            output.extend(b"\n")
    return bytes(output)


def generate_third_party_licenses(frontend_dir: Path, dist_dir: Path) -> Path:
    dist_dir.mkdir(parents=True, exist_ok=True)
    output_path = dist_dir / LICENSE_BUNDLE_NAME
    output_path.write_bytes(render_third_party_licenses(frontend_dir))
    return output_path


def node_major_version() -> int:
    try:
        result = subprocess.run(
            ["node", "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ControlUIError(
            f"Node.js {MINIMUM_NODE_MAJOR}+ is required to build the Control UI"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ControlUIError("Could not determine the installed Node.js version") from exc

    version = result.stdout.strip()
    match = NODE_VERSION_RE.fullmatch(version)
    if match is None:
        raise ControlUIError(f"Could not parse Node.js version: {version!r}")
    return int(match.group("major"))


def require_supported_node() -> None:
    major = node_major_version()
    if major < MINIMUM_NODE_MAJOR:
        raise ControlUIError(
            f"Node.js {MINIMUM_NODE_MAJOR}+ is required; found Node.js {major}"
        )


def _resolve_npm_command() -> str:
    candidates = ("npm.cmd", "npm") if IS_WINDOWS else ("npm",)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    raise ControlUIError("Required command is unavailable: npm")


def _run_npm(args: list[str], frontend_dir: Path) -> None:
    if not args or args[0] != "npm":
        command = args[0] if args else "npm"
        raise ControlUIError(f"Unsupported npm command: {command}")

    resolved_args = [_resolve_npm_command(), *args[1:]]
    env = os.environ.copy()
    env.update(
        {
            "CI": "1",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
        }
    )
    try:
        subprocess.run(resolved_args, cwd=frontend_dir, env=env, check=True)
    except FileNotFoundError as exc:
        raise ControlUIError("Required command is unavailable: npm") from exc
    except subprocess.CalledProcessError as exc:
        raise ControlUIError(
            f"Command failed with exit code {exc.returncode}: {' '.join(args)}"
        ) from exc


def _runtime_text_files(dist_dir: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(dist_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in RUNTIME_TEXT_SUFFIXES
    )


def _verify_index_assets(index_path: Path, assets_dir: Path) -> None:
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ControlUIError(f"Control UI entry point is missing: {index_path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ControlUIError(f"Could not read Control UI entry point {index_path}: {exc}") from exc
    if not index_text.strip():
        raise ControlUIError(f"Control UI entry point is empty: {index_path}")
    _verify_base_marker(index_text, str(index_path))
    if not assets_dir.is_dir():
        raise ControlUIError(f"Control UI assets directory is missing: {assets_dir}")

    assets = tuple(path for path in sorted(assets_dir.rglob("*")) if path.is_file())
    if not any(path.suffix == ".js" for path in assets):
        raise ControlUIError(f"Control UI has no JavaScript assets in {assets_dir}")
    if not any(path.suffix == ".css" for path in assets):
        raise ControlUIError(f"Control UI has no CSS assets in {assets_dir}")

    references = ASSET_REFERENCE_RE.findall(index_text)
    for filename in REQUIRED_DIST_FILES:
        if not (index_path.parent / filename).is_file():
            raise ControlUIError(f"Required Control UI runtime file is missing: {filename}")
        if not any(reference.removeprefix("./") == filename for reference in references):
            raise ControlUIError(
                f"{index_path} does not reference required runtime file {filename}"
            )

    referenced_assets: list[Path] = []
    for reference in references:
        if not reference.startswith(("./assets/", "assets/")):
            continue
        relative = reference.removeprefix("./")
        target = (index_path.parent / relative).resolve()
        if index_path.parent.resolve() not in target.parents:
            raise ControlUIError(f"Unsafe asset reference in {index_path}: {reference}")
        if not target.is_file():
            raise ControlUIError(f"Referenced Control UI asset is missing: {target}")
        referenced_assets.append(target)
    if not any(path.suffix == ".js" for path in referenced_assets):
        raise ControlUIError(f"{index_path} does not reference a JavaScript asset")
    if not any(path.suffix == ".css" for path in referenced_assets):
        raise ControlUIError(f"{index_path} does not reference a CSS asset")


def _verify_base_marker(index_text: str, location: str) -> None:
    marker_count = len(BASE_MARKER_RE.findall(index_text))
    if marker_count != 1:
        raise ControlUIError(
            f"Control UI index must contain exactly one "
            f"<base data-agentos-control-base> marker; found {marker_count} in {location}"
        )


def _verify_runtime_references(dist_dir: Path) -> None:
    for path in _runtime_text_files(dist_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ControlUIError(f"Could not inspect generated asset {path}: {exc}") from exc
        if "/control" in text:
            raise ControlUIError(
                "Generated Control UI hard-codes /control instead of using its "
                f"runtime base: {path}"
            )
        for legacy_reference in LEGACY_REFERENCES:
            if legacy_reference in text:
                raise ControlUIError(
                    f"Generated Control UI contains legacy reference "
                    f"{legacy_reference!r}: {path}"
                )


def _verify_license_bundle(dist_dir: Path, lock_path: Path) -> None:
    bundle_path = dist_dir / LICENSE_BUNDLE_NAME
    try:
        bundle = bundle_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ControlUIError(f"Control UI license bundle is missing: {bundle_path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ControlUIError(
            f"Could not read Control UI license bundle {bundle_path}: {exc}"
        ) from exc
    if not bundle.startswith(LICENSE_BUNDLE_PREAMBLE):
        raise ControlUIError(f"Control UI license bundle has an invalid preamble: {bundle_path}")

    actual_packages = {
        line.removeprefix("Package: ")
        for line in bundle.splitlines()
        if line.startswith("Package: ")
    }
    expected_packages = {package.identifier for package in production_packages(lock_path)}
    if actual_packages != expected_packages:
        missing = sorted(expected_packages - actual_packages)
        unexpected = sorted(actual_packages - expected_packages)
        raise ControlUIError(
            f"Control UI license bundle is out of date: missing={missing}, "
            f"unexpected={unexpected}"
        )


def verify_control_ui(repo_root: Path) -> None:
    frontend_dir = repo_root / "frontend"
    dist_dir = repo_root / "src" / "agentos" / "gateway" / "static" / "dist"
    _verify_index_assets(dist_dir / "index.html", dist_dir / "assets")
    _verify_runtime_references(dist_dir)
    _verify_license_bundle(dist_dir, frontend_dir / "package-lock.json")


def _normalized_archive_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _verify_archive_contents(
    archive_path: Path,
    raw_names: list[str],
    read_member: Callable[[str], bytes],
) -> None:
    names_by_normalized: dict[str, str] = {}
    for raw_name in raw_names:
        name = _normalized_archive_name(raw_name)
        if not name or name.endswith("/"):
            continue
        if name in names_by_normalized:
            raise ControlUIError(f"Archive contains duplicate path {name!r}: {archive_path}")
        names_by_normalized[name] = raw_name

        parts = name.split("/")
        if "node_modules" in parts:
            raise ControlUIError(f"Archive contains node_modules entry {name!r}: {archive_path}")
        padded_name = f"/{name}"
        for forbidden_path in FORBIDDEN_ARCHIVE_PATH_PARTS:
            if forbidden_path in padded_name:
                raise ControlUIError(
                    f"Archive contains legacy Control UI entry {name!r}: {archive_path}"
                )

    index_candidates = [
        name
        for name in names_by_normalized
        if name == WHEEL_DIST_INDEX or name.endswith(SDIST_DIST_INDEX_SUFFIX)
    ]
    if len(index_candidates) != 1:
        raise ControlUIError(
            f"Expected one packaged Control UI index in {archive_path}, "
            f"found {len(index_candidates)}"
        )

    index_name = index_candidates[0]
    dist_root = index_name.removesuffix("/index.html")
    license_name = f"{dist_root}/{LICENSE_BUNDLE_NAME}"
    if license_name not in names_by_normalized:
        raise ControlUIError(f"Archive is missing {license_name}: {archive_path}")

    index_bytes = read_member(names_by_normalized[index_name])
    try:
        index_text = index_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ControlUIError(f"Archive Control UI index is not UTF-8: {archive_path}") from exc
    if not index_text.strip():
        raise ControlUIError(f"Archive Control UI index is empty: {archive_path}")
    _verify_base_marker(index_text, f"{archive_path}:{index_name}")
    index_references = ASSET_REFERENCE_RE.findall(index_text)

    for filename in REQUIRED_DIST_FILES:
        required_name = f"{dist_root}/{filename}"
        if required_name not in names_by_normalized:
            raise ControlUIError(f"Archive is missing {required_name}: {archive_path}")
        if not any(reference.removeprefix("./") == filename for reference in index_references):
            raise ControlUIError(
                f"Archive index does not reference required runtime file "
                f"{filename}: {archive_path}"
            )

    asset_prefix = f"{dist_root}/assets/"
    javascript_assets = sorted(
        name
        for name in names_by_normalized
        if name.startswith(asset_prefix) and name.endswith(".js")
    )
    stylesheet_assets = sorted(
        name
        for name in names_by_normalized
        if name.startswith(asset_prefix) and name.endswith(".css")
    )
    if not javascript_assets:
        raise ControlUIError(f"Archive has no packaged Control UI JavaScript: {archive_path}")
    if not stylesheet_assets:
        raise ControlUIError(f"Archive has no packaged Control UI CSS: {archive_path}")

    license_bytes = read_member(names_by_normalized[license_name])
    if not license_bytes.startswith(LICENSE_BUNDLE_PREAMBLE.encode()):
        raise ControlUIError(f"Archive has an invalid Control UI license bundle: {archive_path}")

    referenced_assets = {
        f"{dist_root}/{reference.removeprefix('./')}"
        for reference in index_references
        if reference.startswith(("./assets/", "assets/"))
    }
    missing_references = sorted(referenced_assets - names_by_normalized.keys())
    if missing_references:
        raise ControlUIError(
            f"Archive index references missing assets in {archive_path}: {missing_references}"
        )
    if not any(name.endswith(".js") for name in referenced_assets):
        raise ControlUIError(f"Archive index references no JavaScript asset: {archive_path}")
    if not any(name.endswith(".css") for name in referenced_assets):
        raise ControlUIError(f"Archive index references no CSS asset: {archive_path}")

    runtime_names = [
        name
        for name in names_by_normalized
        if (
            name == index_name
            or name.startswith(asset_prefix)
            or name in {f"{dist_root}/{filename}" for filename in REQUIRED_DIST_FILES}
        )
        and Path(name).suffix.lower() in RUNTIME_TEXT_SUFFIXES
    ]
    for name in runtime_names:
        try:
            text = read_member(names_by_normalized[name]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlUIError(
                f"Archive Control UI asset is not UTF-8 ({name}): {archive_path}"
            ) from exc
        if "/control" in text:
            raise ControlUIError(
                f"Archive Control UI hard-codes /control in {name}: {archive_path}"
            )
        for legacy_reference in LEGACY_REFERENCES:
            if legacy_reference in text:
                raise ControlUIError(
                    f"Archive Control UI contains legacy reference "
                    f"{legacy_reference!r} in {name}: {archive_path}"
                )


def verify_archive(archive_path: Path) -> None:
    """Verify packaged UI contents without extracting an untrusted archive."""

    if not archive_path.is_file():
        raise ControlUIError(f"Build archive is missing: {archive_path}")
    if archive_path.suffix == ".whl":
        try:
            with zipfile.ZipFile(archive_path) as archive:
                _verify_archive_contents(archive_path, archive.namelist(), archive.read)
        except zipfile.BadZipFile as exc:
            raise ControlUIError(f"Invalid wheel archive: {archive_path}") from exc
        return
    if archive_path.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        try:
            with tarfile.open(archive_path, "r:*") as archive:

                def read_tar_member(name: str) -> bytes:
                    member = archive.getmember(name)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ControlUIError(
                            f"Could not read archive member {name!r}: {archive_path}"
                        )
                    return extracted.read()

                _verify_archive_contents(
                    archive_path,
                    archive.getnames(),
                    read_tar_member,
                )
        except tarfile.TarError as exc:
            raise ControlUIError(f"Invalid source archive: {archive_path}") from exc
        return
    raise ControlUIError(f"Unsupported build archive type: {archive_path}")


def build_control_ui(repo_root: Path) -> None:
    frontend_dir = repo_root / "frontend"
    dist_dir = repo_root / "src" / "agentos" / "gateway" / "static" / "dist"
    require_supported_node()
    _run_npm(["npm", "ci"], frontend_dir)
    _run_npm(["npm", "run", "build"], frontend_dir)
    generate_third_party_licenses(frontend_dir, dist_dir)
    verify_control_ui(repo_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify", "verify-archive"))
    parser.add_argument("archives", nargs="*", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = repo_root_from_script()
    try:
        if args.command == "build":
            if args.archives:
                raise ControlUIError("build does not accept archive paths")
            build_control_ui(repo_root)
        elif args.command == "verify":
            if args.archives:
                raise ControlUIError("verify does not accept archive paths")
            verify_control_ui(repo_root)
        else:
            if not args.archives:
                raise ControlUIError("verify-archive requires at least one archive path")
            for archive_path in args.archives:
                verify_archive(archive_path.resolve())
    except ControlUIError as exc:
        print(f"Control UI {args.command} failed: {exc}", file=sys.stderr)
        return 1
    print(f"Control UI {args.command} passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
