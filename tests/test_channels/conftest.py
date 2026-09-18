from __future__ import annotations

from pathlib import Path

import pytest

from agentos.channels import msteams


@pytest.fixture(autouse=True)
def _msteams_default_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the Teams conversation cache out of the real ``~/.agentos``.

    ``MSTeamsChannel`` saves that cache on every inbound turn, and most tests
    build the channel without a ``workspace_dir``, which falls back to the
    user's home directory.
    """
    monkeypatch.setattr(msteams, "_default_workspace_dir", lambda: tmp_path / "agentos-home")
