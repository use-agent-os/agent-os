"""Regression tests for retired channel adapter config migration."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from agentos.gateway.config import GatewayConfig
from agentos.gateway.config_migration import migrate_config_payload


def _supported_entry(name: str, token: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "telegram",
        "token": token,
        "approved_sender_ids": ["123"],
    }


@pytest.mark.parametrize(
    ("raw_type", "canonical_type"),
    [
        ("dingtalk", "dingtalk"),
        (" MATRIX ", "matrix"),
        ("qq", "qq"),
        ("qqbot", "qq"),
        ("QQ_bot", "qq"),
        ("qq-bot", "qq"),
        ("wecom", "wecom"),
    ],
)
def test_retired_channel_types_and_qq_aliases_are_removed_before_validation(
    raw_type: str,
    canonical_type: str,
) -> None:
    supported_before = _supported_entry("primary", "supported-secret")
    supported_after = _supported_entry("secondary", "another-supported-secret")
    source = {
        "channels": {
            "channels": [
                supported_before,
                {
                    "name": "retired",
                    "type": raw_type,
                    "client_secret": "must-never-be-reported",
                    "nested": {"credential": "also-secret"},
                },
                supported_after,
            ]
        }
    }

    result = migrate_config_payload(source)

    assert result.changed is True
    assert result.payload["channels"]["channels"] == [supported_before, supported_after]
    assert source["channels"]["channels"][1]["type"] == raw_type
    assert result.changes == (
        f"channels.channels: removed 1 retired channel adapter entry ({canonical_type}=1)",
    )
    report = json.dumps(
        {
            "changes": result.changes,
            "warnings": result.warnings,
            "removed_fields": result.removed_fields,
        }
    )
    assert "must-never-be-reported" not in report
    assert "also-secret" not in report
    assert "supported-secret" not in report


def test_retired_channel_migration_is_order_preserving_and_idempotent() -> None:
    first = _supported_entry("first", "first-secret")
    second = _supported_entry("second", "second-secret")
    source = {
        "channels": {
            "channels": [
                {"name": "old-dingtalk", "type": "dingtalk", "client_secret": "secret-a"},
                first,
                {"name": "old-matrix", "type": "matrix", "password": "secret-b"},
                {"name": "old-qq", "type": "qqbot", "app_secret": "secret-c"},
                second,
                {"name": "old-wecom", "type": "wecom", "corp_secret": "secret-d"},
            ]
        }
    }

    first_result = migrate_config_payload(source)
    second_result = migrate_config_payload(first_result.payload)

    assert first_result.payload["channels"]["channels"] == [first, second]
    assert first_result.changes == (
        "channels.channels: removed 4 retired channel adapter entries "
        "(dingtalk=1, matrix=1, qq=1, wecom=1)",
    )
    assert second_result.payload == first_result.payload
    assert second_result.changed is False
    assert second_result.changes == ()


def test_load_rewrites_retired_channels_with_secure_backup_and_safe_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[[channels.channels]]",
                'name = "primary"',
                'type = "telegram"',
                'token = "supported-token-secret"',
                "",
                "[[channels.channels]]",
                'name = "legacy-matrix"',
                'type = "matrix"',
                'homeserver_url = "https://matrix.example"',
                'access_token = "retired-access-token-secret"',
                "",
                "[[channels.channels]]",
                'name = "secondary"',
                'type = "telegram"',
                'token = "second-supported-token-secret"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    caplog.set_level("WARNING", logger="agentos.gateway.config_migration")

    config = GatewayConfig.load(config_path)

    assert [entry.name for entry in config.channels.channels] == ["primary", "secondary"]
    migrated_text = config_path.read_text(encoding="utf-8")
    assert "legacy-matrix" not in migrated_text
    assert "retired-access-token-secret" not in migrated_text
    assert migrated_text.index('name = "primary"') < migrated_text.index('name = "secondary"')
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    backups = list(tmp_path.glob("config.toml.backup.*"))
    assert len(backups) == 1
    assert "retired-access-token-secret" in backups[0].read_text(encoding="utf-8")
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600

    migration_records = [
        record
        for record in caplog.records
        if record.getMessage() == "AgentOS config migrated for 0.2.0 schema"
    ]
    assert len(migration_records) == 1
    record = migration_records[0]
    report = json.dumps(
        {
            "message": record.getMessage(),
            "changes": record.changes,
            "removed_fields": record.removed_fields,
            "warnings": record.warnings,
        }
    )
    assert "retired-access-token-secret" not in report
    assert "supported-token-secret" not in report
    assert "legacy-matrix" not in report
    assert record.changes == [
        "channels.channels: removed 1 retired channel adapter entry (matrix=1)"
    ]
