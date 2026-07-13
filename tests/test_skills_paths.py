from __future__ import annotations

from agentos.skills.paths import resolve_skill_layer_dirs


def test_default_managed_dir_is_kept_before_directory_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))

    layer_dirs = resolve_skill_layer_dirs(allow_bundled=False)

    assert layer_dirs.managed_dir == tmp_path / "skills"
    assert not layer_dirs.managed_dir.exists()
