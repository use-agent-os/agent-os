from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(".github/workflows")
CLASSIFIER = Path(".github/scripts/classify-ci-changes.sh")
LISTER = ".github/scripts/list-ci-changed-files.sh"
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
    assert LISTER in text
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


def test_ci_change_classifier_treats_control_ui_as_runtime_and_release_surface(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "frontend/src/App.tsx",
            "frontend/package-lock.json",
            "scripts/build_control_ui.py",
            "src/agentos/gateway/control_ui.py",
        ],
    )

    assert outputs["docs_only"] == "false"
    assert outputs["runtime_changed"] == "true"
    assert outputs["test_changed"] == "false"
    assert outputs["ci_changed"] == "false"
    assert outputs["dependency_changed"] == "false"
    assert outputs["release_changed"] == "true"


def test_ci_change_classifier_tracks_control_ui_release_contract_files(
    tmp_path: Path,
) -> None:
    outputs = _classify_changed_files(
        tmp_path,
        [
            "Dockerfile",
            ".dockerignore",
            ".gitignore",
            "src/agentos/gateway/boot.py",
            "THIRD_PARTY_NOTICES.md",
            "tests/test_scripts/test_build_control_ui.py",
            "tests/test_frontend_third_party_notices.py",
        ],
    )

    assert outputs["docs_only"] == "false"
    assert outputs["runtime_changed"] == "true"
    assert outputs["test_changed"] == "true"
    assert outputs["ci_changed"] == "false"
    assert outputs["dependency_changed"] == "false"
    assert outputs["release_changed"] == "true"


def test_manual_workflows_reference_existing_test_files() -> None:
    for text in _workflow_texts():
        for raw_path in TEST_PATH_RE.findall(text):
            assert Path(raw_path).is_file(), f"workflow references missing test: {raw_path}"


def test_webui_browser_workflow_runs_for_relevant_prs_without_credentials() -> None:
    data = _workflow("webui-browser-smoke.yml")
    text = (WORKFLOW_DIR / "webui-browser-smoke.yml").read_text(encoding="utf-8")

    assert _trigger_keys(data) == {"pull_request", "push", "workflow_dispatch"}
    pull_request = data["on"]["pull_request"]
    assert "frontend/**" in pull_request["paths"]
    assert "scripts/build_control_ui.py" in pull_request["paths"]
    assert "src/agentos/gateway/control_ui.py" in pull_request["paths"]
    assert "tests/functional/test_webui_browser*.py" in pull_request["paths"]
    assert 'AGENTOS_WEBUI_BROWSER_E2E: "1"' in text
    assert 'AGENTOS_WEBUI_BROWSER_CHAT_E2E: "1"' in text
    assert "tests/functional/test_webui_browser_e2e.py" in text
    assert "tests/functional/test_webui_browser_chat_e2e.py" in text
    assert "npm --prefix frontend exec -- playwright install chromium" in text
    assert "OPENROUTER_API_KEY" not in text
    assert "secrets." not in text


def test_browser_smoke_uses_one_exact_lockfile_pinned_playwright_installation() -> None:
    manifest = json.loads(Path("frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads(Path("frontend/package-lock.json").read_text(encoding="utf-8"))
    version = manifest["devDependencies"]["playwright"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert lock["packages"][""]["devDependencies"]["playwright"] == version
    assert lock["packages"]["node_modules/playwright"]["version"] == version

    for test_path in (
        Path("tests/functional/test_webui_browser_e2e.py"),
        Path("tests/functional/test_webui_browser_chat_e2e.py"),
    ):
        text = test_path.read_text(encoding="utf-8")
        assert 'FRONTEND_DIR / "node_modules"' in text
        assert '"install"' not in text


def test_ubuntu_quality_runs_frontend_source_gate_without_rebuilding() -> None:
    data = _workflow("ci.yml")
    steps = data["jobs"]["ubuntu-quality"]["steps"]
    commands = [step.get("run") for step in steps if isinstance(step, dict)]

    assert commands.count("npm --prefix frontend run check") == 1
    assert commands.count("python scripts/build_control_ui.py build") == 1


def test_control_ui_is_built_before_runtime_and_release_validation() -> None:
    shared_build = "python scripts/build_control_ui.py build"
    workflows = {
        "ci.yml": 3,
        "frontend.yml": 1,
        "pypi-publish.yml": 1,
        "wheelhouse-release.yml": 1,
        "webui-browser-smoke.yml": 1,
        "live-release-e2e.yml": 1,
    }

    for name, minimum_count in workflows.items():
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert text.count(shared_build) >= minimum_count, (
            f"{name} must build and verify the generated Control UI"
        )
        assert 'node-version: "22"' in text, f"{name} must use the supported Node runtime"


def test_distribution_workflows_verify_control_ui_inside_built_archives() -> None:
    ci = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    pypi = (WORKFLOW_DIR / "pypi-publish.yml").read_text(encoding="utf-8")

    assert "verify-archive dist/*.whl" in ci
    assert "verify-archive dist/*.whl dist/*.tar.gz" in pypi


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
    assert "tests/functional/test_live_channel_telegram_smoke.py" in text
    assert "tests/functional/test_webui_browser_chat_e2e.py" not in text
    assert "playwright install chromium" not in text
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


LFS_CACHE_JOBS = ("ubuntu-quality", "windows-compat", "release-packaging")


def _steps(workflow: str, job: str) -> list[dict]:
    steps = _workflow(workflow)["jobs"][job]["steps"]
    return [step for step in steps if isinstance(step, dict)]


def test_ci_restores_git_lfs_objects_from_cache_instead_of_refetching() -> None:
    """The three jobs that need the ONNX weights must not re-download them.

    The bundle is ~45 MB (bge + MiniLM + pilot) and every CI run checked it
    out three times, which is what exhausted the account's Git LFS bandwidth.
    Each job now checks out WITHOUT LFS, restores ``.git/lfs/objects`` from
    the Actions cache (free, and not billed as LFS bandwidth), and only falls
    back to a network ``git lfs pull`` on a cache miss.
    """
    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

    for job in LFS_CACHE_JOBS:
        steps = _steps("ci.yml", job)
        names = [step.get("name") for step in steps]

        checkout = next(
            step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")
        )
        assert checkout.get("with", {}).get("lfs") is not True, (
            f"{job} must not hydrate LFS at checkout; it restores the cache instead"
        )

        assert "Compute Git LFS cache key" in names, f"{job} must key the cache on the LFS oids"
        assert "Restore Git LFS objects cache" in names, f"{job} must restore the LFS cache"
        assert "Hydrate Git LFS objects" in names, f"{job} must materialize the weights"

        assert (
            names.index("Compute Git LFS cache key")
            < names.index("Restore Git LFS objects cache")
            < names.index("Hydrate Git LFS objects")
        ), f"{job} must compute the key, restore the cache, then hydrate, in that order"

        # Hydration has to happen before anything imports the models.
        assert names.index("Hydrate Git LFS objects") < names.index("Install dependencies"), (
            f"{job} must hydrate the weights before installing dependencies"
        )

    assert "actions/cache@v6" in text
    assert "path: .git/lfs/objects" in text
    # Cache hit must take the offline path; only a miss touches the network.
    assert "git lfs checkout" in text
    assert "git lfs pull" in text


def test_ci_lfs_hydration_is_verified_not_assumed() -> None:
    """A cache restore that silently yields pointer files would turn the real
    ONNX tests into confusing parse errors deep in the suite. Each job asserts
    the weights are hydrated right after the hydrate step instead."""
    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

    assert text.count("Verify Git LFS hydration") == len(LFS_CACHE_JOBS)
    assert "git-lfs.github.com/spec/v1" in text


def test_release_workflows_still_hydrate_lfs_at_checkout() -> None:
    """The CI cache is a bandwidth optimization for the test jobs only. The
    published-artifact workflows keep the direct ``lfs: true`` + ``git lfs
    pull`` path, because their hydration asserts are the last line of defense
    against shipping a 130-byte pointer inside a wheel."""
    for name in ("wheelhouse-release.yml", "pypi-publish.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert "lfs: true" in text, f"{name} must hydrate LFS directly at checkout"


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


def test_wheelhouse_release_dropped_v4_router_bundle() -> None:
    """The legacy v4_phase3 model bundle was removed from the tree to slim
    the wheel; the release must still hydrate the pilot bundle and must
    guard against the legacy bundle sneaking back into the wheel."""
    text = (WORKFLOW_DIR / "wheelhouse-release.yml").read_text(encoding="utf-8")

    assert 'git lfs pull --include="src/agentos/agentos_router/models/**"' in text
    assert 'bundle / "lgbm_main.bin"' not in text
    assert "legacy v4_phase3 bundle leaked back into the wheel" in text


def test_pypi_publish_dropped_v4_router_bundle() -> None:
    text = (WORKFLOW_DIR / "pypi-publish.yml").read_text(encoding="utf-8")

    assert 'git lfs pull --include="src/agentos/memory/models/**"' in text
    assert 'git lfs pull --include="src/agentos/agentos_router/models/**"' in text
    # The v4 hydration asserts are gone, replaced by the same anti-regression
    # guard the wheelhouse release carries.
    assert "lgbm_main.bin" not in text
    assert "legacy v4_phase3 bundle leaked back into the tree" in text


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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _repo_with_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    """Build a throwaway repo and return ``(path, base_sha, head_sha)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "ci@example.test")
    _git(repo, "config", "user.name", "CI Test")

    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "src").mkdir()
    (repo / "src" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "src/thing.py")
    _git(repo, "commit", "-qm", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")

    return repo, base_sha, head_sha


def _list_changed_files(
    repo: Path,
    tmp_path: Path,
    *,
    event_name: str,
    base_sha: str = "",
    head_sha: str = "",
) -> tuple[list[str], subprocess.CompletedProcess[str]]:
    changed_file = tmp_path / "changed-files.txt"
    lister = Path(LISTER).resolve()

    result = subprocess.run(
        [
            _bash_executable(),
            lister.as_posix(),
            event_name,
            base_sha,
            head_sha,
            changed_file.as_posix(),
        ],
        cwd=repo,
        check=False,
        text=True,
        capture_output=True,
    )
    if not changed_file.exists():
        return [], result
    listed = [line for line in changed_file.read_text(encoding="utf-8").splitlines() if line]
    return listed, result


def test_changed_file_lister_diffs_a_pull_request_range(tmp_path: Path) -> None:
    repo, base_sha, head_sha = _repo_with_two_commits(tmp_path)

    listed, result = _list_changed_files(
        repo,
        tmp_path,
        event_name="pull_request",
        base_sha=base_sha,
        head_sha=head_sha,
    )

    assert result.returncode == 0, result.stderr
    assert listed == ["src/thing.py"]


def test_changed_file_lister_runs_everything_for_a_push(tmp_path: Path) -> None:
    repo, _base_sha, _head_sha = _repo_with_two_commits(tmp_path)

    listed, result = _list_changed_files(repo, tmp_path, event_name="push")

    assert result.returncode == 0, result.stderr
    assert listed == [".ci/run-all"]


def test_changed_file_lister_runs_everything_when_the_head_sha_is_unreachable(
    tmp_path: Path,
) -> None:
    """A head sha that is not in the object DB must not hard-fail the pipeline.

    Reproduces CI run #830: a fork PR whose workflow was released only after the
    PR had been merged. ``refs/pull/<n>/merge`` was already gone, so
    ``actions/checkout`` silently fell back to ``origin/main`` and the head sha
    lived only in the contributor's fork. ``git diff`` then exited 128 with
    ``fatal: bad object`` and failed CI on a classification step.
    """
    repo, base_sha, _head_sha = _repo_with_two_commits(tmp_path)
    missing_sha = "da1445ade0d8d94a55002a298d5bcb2e78baaca3"

    listed, result = _list_changed_files(
        repo,
        tmp_path,
        event_name="pull_request",
        base_sha=base_sha,
        head_sha=missing_sha,
    )

    assert result.returncode == 0, result.stderr
    assert listed == [".ci/run-all"]


def test_changed_file_lister_runs_everything_when_the_base_sha_is_unreachable(
    tmp_path: Path,
) -> None:
    repo, _base_sha, head_sha = _repo_with_two_commits(tmp_path)
    missing_sha = "0000000000000000000000000000000000000001"

    listed, result = _list_changed_files(
        repo,
        tmp_path,
        event_name="pull_request",
        base_sha=missing_sha,
        head_sha=head_sha,
    )

    assert result.returncode == 0, result.stderr
    assert listed == [".ci/run-all"]


def test_changed_file_lister_runs_everything_when_a_pull_request_sha_is_blank(
    tmp_path: Path,
) -> None:
    repo, _base_sha, head_sha = _repo_with_two_commits(tmp_path)

    listed, result = _list_changed_files(
        repo,
        tmp_path,
        event_name="pull_request",
        base_sha="",
        head_sha=head_sha,
    )

    assert result.returncode == 0, result.stderr
    assert listed == [".ci/run-all"]


def test_ci_lists_changed_files_through_the_shared_script() -> None:
    """ci.yml must not inline an unguarded ``git diff`` of the PR range."""
    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

    assert LISTER in text
    assert "git diff --name-only" not in text
