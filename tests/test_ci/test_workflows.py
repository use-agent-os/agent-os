from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(".github/workflows")
CLASSIFIER = Path(".github/scripts/classify-ci-changes.sh")
TEST_PATH_RE = re.compile(r"tests/[A-Za-z0-9_./-]+\.py")


def _workflow(name: str) -> dict:
    path = WORKFLOW_DIR / name
    assert path.is_file(), f"missing workflow: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trigger_keys(data: dict) -> set[str]:
    triggers = data.get("on", {})
    if triggers is None:
        return set()
    if isinstance(triggers, str):
        return {triggers}
    return set(triggers)


def _workflow_texts() -> list[str]:
    return [path.read_text(encoding="utf-8") for path in WORKFLOW_DIR.glob("*.yml")]


def _is_windows_wsl_bash(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.endswith("/windows/system32/bash.exe")


def _bash_executable(
    *,
    os_name: str = os.name,
    path_lookup: Callable[[str], str | None] = shutil.which,
    exists: Callable[[Path], bool] = Path.is_file,
    program_files: str | None = None,
) -> str:
    found = path_lookup("bash")
    if os_name != "nt":
        return found or "bash"

    candidates: list[Path] = []
    if found and not _is_windows_wsl_bash(found):
        candidates.append(Path(found))

    git_root = Path(program_files or os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
    candidates.extend(
        [
            git_root / "bin" / "bash.exe",
            git_root / "usr" / "bin" / "bash.exe",
        ]
    )

    for candidate in candidates:
        if exists(candidate):
            return str(candidate)

    raise AssertionError("Git Bash is required to run the CI change classifier on Windows")


def _classify_changed_files(
    tmp_path: Path,
    paths: list[str],
    *,
    line_ending: str = "\n",
) -> dict[str, str]:
    changed_file = tmp_path / "changed-files.txt"
    output_file = tmp_path / "github-output.txt"
    changed_file.write_text(
        line_ending.join(paths) + line_ending,
        encoding="utf-8",
        newline="",
    )

    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = output_file.as_posix()
    subprocess.run(
        [_bash_executable(), CLASSIFIER.as_posix(), changed_file.as_posix()],
        check=True,
        env=env,
        text=True,
        capture_output=True,
    )

    outputs: dict[str, str] = {}
    for line in output_file.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        outputs[key] = value
    return outputs


def test_default_ci_blocks_pull_requests_and_main_and_dev_pushes() -> None:
    ci_path = WORKFLOW_DIR / "ci.yml"
    if not ci_path.exists():
        return

    data = _workflow("ci.yml")
    text = ci_path.read_text(encoding="utf-8")

    assert {"pull_request", "push", "workflow_dispatch"} <= _trigger_keys(data)
    assert "branches: [main, dev]" in text
    assert "PYTHONPATH: ${{ github.workspace }}" in text
    assert "Configure runtime directories" in text
    assert 'AGENTOS_STATE_DIR=%s/agentos-state\\n' in text
    assert 'AGENTOS_LOG_DIR=%s/agentos-logs\\n' in text
    assert "AGENTOS_TURN_CALL_LOG: \"0\"" in text
    assert "actionlint@v1.7.12" in text
    assert "Classify changed files" in text
    assert "Ubuntu quality gate" in text
    assert "Windows compatibility tests" in text
    assert "Release packaging contracts" in text
    assert "CI result" in text
    assert 'push)\n              printf \'.ci/run-all\\n\' > "${changed_files}"' in text
    assert "runtime_changed" in text
    assert "test_changed" in text
    assert "ci_changed" in text
    assert "dependency_changed" in text
    assert "release_changed" in text
    assert "code_changed" not in text
    assert "workflow_changed" not in text


def test_ci_change_classifier_allows_root_and_docs_markdown_only(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "README.md",
            "CHANGELOG.md",
            "docs/features/skills.md",
            ".github/pull_request_template.md",
        ],
    )

    assert outputs == {
        "docs_only": "true",
        "runtime_changed": "false",
        "test_changed": "false",
        "ci_changed": "false",
        "dependency_changed": "false",
        "release_changed": "false",
    }


def test_classifier_helper_prefers_git_bash_over_windows_wsl_bash(tmp_path: Path) -> None:
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"

    result = _bash_executable(
        os_name="nt",
        path_lookup=lambda _name: r"C:\Windows\System32\bash.exe",
        exists=lambda path: path == git_bash,
        program_files=str(tmp_path),
    )

    assert result == str(git_bash)


def test_ci_change_classifier_accepts_crlf_changed_files(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["README.md", "docs/features/skills.md"],
        line_ending="\r\n",
    )

    assert outputs["docs_only"] == "true"
    assert outputs["runtime_changed"] == "false"


def test_ci_change_classifier_treats_runtime_markdown_as_runtime(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["src/agentos/identity/templates/bootstrap/AGENTS.md"],
    )

    assert outputs["docs_only"] == "false"
    assert outputs["runtime_changed"] == "true"
    assert outputs["test_changed"] == "false"
    assert outputs["ci_changed"] == "false"
    assert outputs["dependency_changed"] == "false"
    assert outputs["release_changed"] == "false"


def test_ci_change_classifier_tracks_test_changes_separately(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        ["tests/test_ci/test_workflows.py"],
    )

    assert outputs["docs_only"] == "false"
    assert outputs["runtime_changed"] == "false"
    assert outputs["test_changed"] == "true"
    assert outputs["ci_changed"] == "false"
    assert outputs["dependency_changed"] == "false"
    assert outputs["release_changed"] == "false"


def test_ci_change_classifier_tracks_ci_dependency_and_release_changes(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [".github/workflows/ci.yml", ".github/scripts/classify-ci-changes.sh", "uv.lock"],
    )

    assert outputs["docs_only"] == "false"
    assert outputs["runtime_changed"] == "true"
    assert outputs["test_changed"] == "false"
    assert outputs["ci_changed"] == "true"
    assert outputs["dependency_changed"] == "true"
    assert outputs["release_changed"] == "true"


def test_ci_change_classifier_tracks_release_surface_changes(tmp_path: Path) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            ".github/workflows/wheelhouse-release.yml",
            "scripts/build_wheelhouse_zip.py",
            "README.release.md",
            "RELEASES.md",
            "tests/test_scripts/test_build_wheelhouse_zip.py",
        ],
    )

    assert outputs["docs_only"] == "false"
    assert outputs["runtime_changed"] == "true"
    assert outputs["test_changed"] == "true"
    assert outputs["ci_changed"] == "true"
    assert outputs["dependency_changed"] == "false"
    assert outputs["release_changed"] == "true"


def test_manual_workflows_reference_existing_test_files() -> None:
    for text in _workflow_texts():
        for raw_path in TEST_PATH_RE.findall(text):
            assert Path(raw_path).is_file(), f"workflow references missing test: {raw_path}"


def test_webui_browser_workflow_is_manual_and_opt_in() -> None:
    data = _workflow("webui-browser-smoke.yml")
    text = (WORKFLOW_DIR / "webui-browser-smoke.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"workflow_dispatch"}
    assert 'AGENTOS_WEBUI_BROWSER_E2E: "1"' in text
    assert "tests/functional/test_webui_browser_e2e.py" in text
    assert "playwright install chromium" in text


def test_llm_workflow_is_single_manual_smoke() -> None:
    data = _workflow("llm-e2e.yml")
    text = (WORKFLOW_DIR / "llm-e2e.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"workflow_dispatch"}
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in text
    assert "tests/functional/test_llm_smoke.py" in text
    assert "llm_costly" not in text
    assert "tests/functional/test_webui_llm_e2e.py" not in text


def test_live_release_e2e_workflow_is_manual_and_separates_private_inputs() -> None:
    data = _workflow("live-release-e2e.yml")
    text = (WORKFLOW_DIR / "live-release-e2e.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"workflow_dispatch"}
    assert "tests/functional/test_gateway_llm_e2e.py" in text
    assert "tests/functional/test_webui_browser_chat_e2e.py" in text
    assert "tests/functional/test_live_channel_telegram_smoke.py" in text
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in text
    assert (
        "AGENTOS_LIVE_TELEGRAM_BOT_TOKEN: "
        "${{ secrets.AGENTOS_LIVE_TELEGRAM_BOT_TOKEN }}"
    ) in text
    assert (
        "AGENTOS_LIVE_TELEGRAM_CHAT_ID: "
        "${{ secrets.AGENTOS_LIVE_TELEGRAM_CHAT_ID }}"
    ) in text
    assert "tests/private" not in text


def test_default_ci_stays_offline_and_does_not_run_live_gates() -> None:
    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

    assert "OPENROUTER_API_KEY" not in text
    assert "AGENTOS_LIVE_TELEGRAM" not in text
    assert "AGENTOS_GATEWAY_LLM_E2E" not in text
    assert "AGENTOS_WEBUI_BROWSER_E2E" not in text
    assert "AGENTOS_WEBUI_BROWSER_CHAT_E2E" not in text
    assert "test_gateway_llm_e2e.py" not in text
    assert "test_live_channel_telegram_smoke.py" not in text


def test_live_release_e2e_fails_fast_when_required_provider_secret_is_missing() -> None:
    text = (WORKFLOW_DIR / "live-release-e2e.yml").read_text(encoding="utf-8")

    assert "Fail if OpenRouter secret is missing" in text
    assert 'if [ -z "$OPENROUTER_API_KEY" ]; then' in text
    assert "OPENROUTER_API_KEY GitHub secret is required" in text
    assert "Fail if Telegram secrets are missing when channel smoke is enabled" in text
    assert 'if [ -z "$AGENTOS_LIVE_TELEGRAM_BOT_TOKEN" ]' in text
    assert 'if [ -z "$AGENTOS_LIVE_TELEGRAM_CHAT_ID" ]' in text


def test_wheelhouse_release_publishes_only_recommended_router_profile() -> None:
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert "      profile:\n" not in text
    assert "RELEASE_PROFILE: recommended" in text
    assert "--profile \"${RELEASE_PROFILE}\"" in text
    assert "- core" not in text


def test_wheelhouse_release_hydrates_current_embedding_bundle() -> None:
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert 'git lfs pull --include="src/agentos/memory/models/**"' in text
    assert "memory/models/bge_onnx" in text
    assert 'root / "model.onnx"' in text
    assert 'root / "tokenizer.json"' in text
    assert 'root / "vocab.txt"' in text
    # The retired E3b router bundle must not linger in the verify list.
    assert "intent_head.joblib" not in text


def test_wheelhouse_release_hydrates_v4_router_bundle() -> None:
    """strategy="v4_phase3" is the default; a bundle that fails to hydrate
    degrades every turn to the default tier with only a boot warning, so the
    release must fail loudly instead."""
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert 'git lfs pull --include="src/agentos/agentos_router/models/**"' in text
    assert "v4.2_phase3_inference" in text
    assert 'bundle / "lgbm_main.bin"' in text
    assert 'bundle / "features/tfidf.pkl"' in text
    assert 'bundle / "router.runtime.yaml"' in text
    # The bundle shares memory's bge_onnx export rather than carrying a copy.
    assert "v4.2_phase3_inference/bge_onnx" not in text


def test_pypi_publish_hydrates_v4_router_bundle() -> None:
    text = (WORKFLOW_DIR / "pypi-publish.yml").read_text(encoding="utf-8")

    assert 'git lfs pull --include="src/agentos/memory/models/**"' in text
    assert 'git lfs pull --include="src/agentos/agentos_router/models/**"' in text
    assert "v4.2_phase3_inference" in text
    assert "lgbm_main.bin" in text


def test_wheelhouse_release_hydrates_pilot_minilm_export() -> None:
    """T1's MiniLM INT8 export ships in the wheel already; the hydration
    check must guard it exactly like bge_onnx so a non-hydrated LFS checkout
    can't silently ship a ~130-byte pointer file instead of the 23 MB ONNX."""
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert "memory/models/embeddings/all-MiniLM-L6-v2-int8" in text
    assert 'minilm / "model.onnx"' in text
    assert 'minilm / "tokenizer.json"' in text
    assert 'minilm / "vocab.txt"' in text


def test_pypi_publish_hydrates_pilot_minilm_export() -> None:
    text = (WORKFLOW_DIR / "pypi-publish.yml").read_text(encoding="utf-8")

    assert "memory/models/embeddings/all-MiniLM-L6-v2-int8" in text
    assert '"model.onnx"' in text
    assert '"tokenizer.json"' in text
    assert '"vocab.txt"' in text
    assert "(minilm / name).is_file()" in text


def test_wheelhouse_release_smoke_guards_pilot_bundle_in_wheel() -> None:
    """The versioned-wheel smoke step must assert the pilot_v1 bundle is
    packaged AND real (size floor), mirroring the v4 bundle wheel check."""
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert 'pilot = "agentos/agentos_router/models/pilot_v1/"' in text
    assert 'pilot + "model.onnx"' in text
    assert "unhydrated Git LFS pointer" in text


def test_release_hydration_checks_guard_pilot_v1_bundle() -> None:
    """The shipped Pilot production bundle (models/pilot_v1/) is the wheel's
    routing brain once pilot-v1 is the default strategy; a non-hydrated LFS
    checkout that shipped a pointer file instead of model.onnx would silently
    degrade every turn. Both release hydration checks must assert the bundle's
    files are present, exactly like the v4/MiniLM required-files entries. (The
    T7 deferral marker is gone now that the bundle exists.)"""
    for name in ("wheelhouse-release.yml", "pypi-publish.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert "NOTE(T7)" not in text
        assert "pilot_v1" in text
        assert "model.onnx" in text
        assert "manifest.json" in text
