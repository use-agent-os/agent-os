"""updates.notify config: default, round-trip, and public-dict surface."""

from __future__ import annotations

from pathlib import Path

from agentos.gateway.config import GatewayConfig, UpdatesConfig


def test_updates_notify_defaults_true() -> None:
    assert GatewayConfig().updates.notify is True


def test_updates_config_forbids_extra() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UpdatesConfig(notify=True, bogus=1)  # type: ignore[call-arg]


def test_updates_notify_toml_round_trip(tmp_path: Path) -> None:
    cfg = tmp_path / "agentos.toml"
    cfg.write_text("[updates]\nnotify = false\n", encoding="utf-8")
    loaded = GatewayConfig.load_from_toml(cfg)
    assert loaded.updates.notify is False


def test_updates_in_public_dict() -> None:
    public = GatewayConfig().to_public_dict()
    assert public.get("updates") == {"notify": True}
