"""cron.add / cron.update refuse a delivery target the channel cannot use.

Both the shape check and the optional adapter probe run at save time, so an
operator learns about a bad recipient while they are looking at the form —
not from a run that fails ten minutes later.
"""

import asyncio
from types import SimpleNamespace

import pytest

from agentos.gateway import rpc_cron
from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_cron import _handle_cron_add, _handle_cron_update
from agentos.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    SessionTarget,
)

SESSION_KEY = "agent:main:telegram:direct:1245463966"


class _FakeScheduler:
    def __init__(self, job: CronJob | None = None) -> None:
        self.added: dict | None = None
        self.updated: dict | None = None
        self.job = job

    async def add_job(self, **kwargs) -> CronJob:
        self.added = kwargs
        return CronJob(
            id="job-1",
            name=kwargs["name"],
            cron_expr=kwargs.get("schedule_value") or "",
            schedule_raw=kwargs.get("schedule_value") or "",
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs["session_key"],
            origin_session_key=kwargs["origin_session_key"],
            delivery=kwargs.get("delivery") or DeliveryConfig(),
        )

    async def update_job(self, job_id, **patch) -> CronJob:
        self.updated = patch
        assert self.job is not None
        for key, value in patch.items():
            setattr(self.job, key, value)
        return self.job

    async def get_job(self, job_id) -> CronJob | None:
        return self.job if self.job and self.job.id == job_id else None


class _FakeChannelManager:
    def __init__(self, adapter=None) -> None:
        self._adapter = adapter

    def get(self, name):
        return self._adapter


def _existing_job() -> CronJob:
    return CronJob(
        id="job-1",
        name="watchdog",
        cron_expr="600",
        schedule_raw="600",
        handler_key="script_run",
        payload={"kind": "script", "script": "tick.sh"},
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="telegram",
            channel_id="1245463966",
        ),
    )


def _add_params(channel_id: str, **extra) -> dict:
    params = {
        "name": "watchdog",
        "expression": "*/10 * * * *",
        "text": "tick",
        "sessionTarget": "isolated",
        "delivery": {"mode": "channel", "channelName": "telegram", "channelId": channel_id},
    }
    params["delivery"].update(extra)
    return params


def test_add_rejects_a_session_key_as_the_channel_id() -> None:
    scheduler = _FakeScheduler()
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(
            _handle_cron_add(
                _add_params(SESSION_KEY),
                RpcContext(conn_id="test", cron_scheduler=scheduler),
            )
        )
    assert "session key" in str(excinfo.value)
    assert "1245463966" in str(excinfo.value)
    assert scheduler.added is None


def test_add_rejects_a_non_numeric_telegram_target() -> None:
    scheduler = _FakeScheduler()
    with pytest.raises(ValueError):
        asyncio.run(
            _handle_cron_add(
                _add_params("C-team-alerts"),
                RpcContext(conn_id="test", cron_scheduler=scheduler),
            )
        )
    assert scheduler.added is None


def test_add_accepts_a_real_chat_id() -> None:
    scheduler = _FakeScheduler()
    asyncio.run(
        _handle_cron_add(
            _add_params("1245463966"),
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )
    )
    assert scheduler.added is not None


def test_add_validates_the_failure_destination_too() -> None:
    scheduler = _FakeScheduler()
    with pytest.raises(ValueError):
        asyncio.run(
            _handle_cron_add(
                _add_params(
                    "1245463966",
                    failureDestination={
                        "mode": "channel",
                        "channelName": "telegram",
                        "channelId": SESSION_KEY,
                    },
                ),
                RpcContext(conn_id="test", cron_scheduler=scheduler),
            )
        )
    assert scheduler.added is None


def test_update_rejects_a_session_key_as_the_channel_id() -> None:
    scheduler = _FakeScheduler(_existing_job())
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(
            _handle_cron_update(
                {
                    "id": "job-1",
                    "delivery": {
                        "mode": "channel",
                        "channelName": "telegram",
                        "channelId": SESSION_KEY,
                    },
                },
                RpcContext(conn_id="test", cron_scheduler=scheduler),
            )
        )
    assert "session key" in str(excinfo.value)
    assert scheduler.updated is None


def test_update_keeps_a_valid_target() -> None:
    scheduler = _FakeScheduler(_existing_job())
    asyncio.run(
        _handle_cron_update(
            {
                "id": "job-1",
                "delivery": {
                    "mode": "channel",
                    "channelName": "telegram",
                    "channelId": "-1001234567890",
                },
            },
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )
    )
    assert scheduler.updated is not None
    assert scheduler.updated["delivery"].channel_id == "-1001234567890"


# ── adapter probe ───────────────────────────────────────────────────────────


def test_probe_blocks_a_chat_the_adapter_says_does_not_exist() -> None:
    async def probe_target(target: str):
        return False, "chat not found"

    scheduler = _FakeScheduler()
    ctx = RpcContext(conn_id="test", cron_scheduler=scheduler)
    ctx.channel_manager = _FakeChannelManager(SimpleNamespace(probe_target=probe_target))
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(_handle_cron_add(_add_params("999"), ctx))
    assert "chat not found" in str(excinfo.value)
    assert scheduler.added is None


def test_probe_that_confirms_the_chat_lets_the_save_through() -> None:
    async def probe_target(target: str):
        return True, ""

    scheduler = _FakeScheduler()
    ctx = RpcContext(conn_id="test", cron_scheduler=scheduler)
    ctx.channel_manager = _FakeChannelManager(SimpleNamespace(probe_target=probe_target))
    asyncio.run(_handle_cron_add(_add_params("1245463966"), ctx))
    assert scheduler.added is not None


def test_an_adapter_without_a_probe_does_not_block_the_save() -> None:
    scheduler = _FakeScheduler()
    ctx = RpcContext(conn_id="test", cron_scheduler=scheduler)
    ctx.channel_manager = _FakeChannelManager(SimpleNamespace())
    asyncio.run(_handle_cron_add(_add_params("1245463966"), ctx))
    assert scheduler.added is not None


def test_a_probe_that_raises_does_not_block_the_save() -> None:
    # The bot being offline is not evidence that the chat id is wrong.
    async def probe_target(target: str):
        raise RuntimeError("telegram unreachable")

    scheduler = _FakeScheduler()
    ctx = RpcContext(conn_id="test", cron_scheduler=scheduler)
    ctx.channel_manager = _FakeChannelManager(SimpleNamespace(probe_target=probe_target))
    asyncio.run(_handle_cron_add(_add_params("1245463966"), ctx))
    assert scheduler.added is not None


def test_a_probe_that_hangs_does_not_block_the_save(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe_target(target: str):
        await asyncio.sleep(60)
        return False, "never gets here"

    monkeypatch.setattr(rpc_cron, "_TARGET_PROBE_TIMEOUT_SECONDS", 0.01)
    scheduler = _FakeScheduler()
    ctx = RpcContext(conn_id="test", cron_scheduler=scheduler)
    ctx.channel_manager = _FakeChannelManager(SimpleNamespace(probe_target=probe_target))
    asyncio.run(_handle_cron_add(_add_params("1245463966"), ctx))
    assert scheduler.added is not None


# ── delivery alias & best_effort preservation ───────────────────────────────


def test_add_delivery_preserves_recipient_under_to_alias() -> None:
    scheduler = _FakeScheduler()
    params = {
        "name": "watchdog",
        "expression": "*/10 * * * *",
        "text": "tick",
        "sessionTarget": "isolated",
        "delivery": {"mode": "channel", "channelName": "telegram", "to": "1245463966"},
    }
    asyncio.run(
        _handle_cron_add(
            params,
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )
    )
    assert scheduler.added is not None
    delivery = scheduler.added.get("delivery")
    assert delivery is not None
    assert delivery.channel_name == "telegram"
    assert delivery.channel_id == "1245463966"


def test_add_delivery_preserves_best_effort() -> None:
    scheduler = _FakeScheduler()
    params = {
        "name": "watchdog",
        "expression": "*/10 * * * *",
        "text": "tick",
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "channel",
            "channel": "telegram",
            "to": "1245463966",
            "bestEffort": True,
        },
    }
    asyncio.run(
        _handle_cron_add(
            params,
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )
    )
    assert scheduler.added is not None
    delivery = scheduler.added.get("delivery")
    assert delivery is not None
    assert delivery.best_effort is True
    assert delivery.channel_id == "1245463966"


def test_update_delivery_preserves_recipient_under_to_alias() -> None:
    scheduler = _FakeScheduler(_existing_job())
    asyncio.run(
        _handle_cron_update(
            {
                "id": "job-1",
                "delivery": {
                    "to": "987654321",
                },
            },
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )
    )
    assert scheduler.updated is not None
    delivery = scheduler.updated.get("delivery")
    assert delivery is not None
    assert delivery.channel_name == "telegram"
    assert delivery.channel_id == "987654321"


def test_update_delivery_preserves_best_effort() -> None:
    scheduler = _FakeScheduler(_existing_job())
    asyncio.run(
        _handle_cron_update(
            {
                "id": "job-1",
                "delivery": {
                    "bestEffort": True,
                },
            },
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )
    )
    assert scheduler.updated is not None
    delivery = scheduler.updated.get("delivery")
    assert delivery is not None
    assert delivery.best_effort is True
    assert delivery.channel_id == "1245463966"


def test_update_delivery_safe_when_job_delivery_is_none() -> None:
    job = _existing_job()
    job.delivery = None  # type: ignore[assignment]
    scheduler = _FakeScheduler(job)
    asyncio.run(
        _handle_cron_update(
            {
                "id": "job-1",
                "delivery": {
                    "channel": "telegram",
                    "to": "1245463966",
                    "best_effort": True,
                },
            },
            RpcContext(conn_id="test", cron_scheduler=scheduler),
        )
    )
    assert scheduler.updated is not None
    delivery = scheduler.updated.get("delivery")
    assert delivery is not None
    assert delivery.channel_name == "telegram"
    assert delivery.channel_id == "1245463966"
    assert delivery.best_effort is True


def test_infer_delivery_preserves_to_and_best_effort() -> None:
    from agentos.scheduler.delivery import infer_delivery

    delivery = asyncio.run(
        infer_delivery(
            session_storage=None,
            session_key="any:key",
            user_overrides={
                "channel": "telegram",
                "to": "1245463966",
                "bestEffort": True,
            },
        )
    )
    assert delivery.mode == DeliveryMode.CHANNEL
    assert delivery.channel_name == "telegram"
    assert delivery.channel_id == "1245463966"
    assert delivery.best_effort is True
