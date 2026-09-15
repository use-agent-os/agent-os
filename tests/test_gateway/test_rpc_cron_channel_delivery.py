"""Issue #2093: channel delivery loses its recipient and best-effort flag.

``cli/cron_cmd.py::_build_delivery_params`` emits ``{"channelName", "to",
"bestEffort"}`` — ``to`` is what the CLI's ``build_delivery()`` produces and
what every channel delivery schema uses. ``_parse_delivery_overrides`` read
only ``channelId`` and never ``bestEffort``, so
``agentos cron add --channel telegram --to <id> --best-effort`` saved a job with
``channel_id=""`` and ``best_effort=False``: the announcement goes nowhere, and
the job is treated as strict delivery so a transient channel error fails it.

The webhook path already accepted ``to``, which is what makes the channel path
a defect rather than a schema choice.

Separately, ``cron.update`` read through ``current_job.delivery`` in three
places, raising ``AttributeError`` when patching delivery on a job that has
none.
"""

from __future__ import annotations

import pytest

from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_cron import (
    _handle_cron_update,
    _parse_delivery_overrides,
)
from agentos.scheduler.delivery import infer_delivery
from agentos.scheduler.types import CronJob, DeliveryConfig, DeliveryMode, ScheduleKind


class _FakeScheduler:
    def __init__(self, job: CronJob) -> None:
        self.job = job
        self.updated: dict | None = None

    async def update_job(self, job_id: str, **patch) -> CronJob:
        self.updated = patch
        for key, value in patch.items():
            setattr(self.job, key, value)
        return self.job

    async def get_job(self, job_id: str) -> CronJob | None:
        return self.job


def _job(delivery: DeliveryConfig | None) -> CronJob:
    return CronJob(
        id="job-A",
        name="watchdog",
        cron_expr="*/10 * * * *",
        schedule_raw="*/10 * * * *",
        schedule_kind=ScheduleKind.CRON,
        handler_key="agent_run",
        delivery=delivery,
    )


async def _update(scheduler: _FakeScheduler, delivery: dict) -> None:
    await _handle_cron_update(
        {"id": "job-A", "delivery": delivery},
        RpcContext(conn_id="test", cron_scheduler=scheduler),
    )


# ── the parser: `to` is the alias the CLI actually sends ────────────────────


def test_to_is_accepted_as_the_channel_id() -> None:
    parsed = _parse_delivery_overrides(
        {"mode": "channel", "channelName": "telegram", "to": "1245463966"}
    )

    assert parsed is not None
    assert parsed["channel_id"] == "1245463966"


def test_channel_id_still_wins_when_both_are_given() -> None:
    """``to`` is an alias, not an override: an explicit ``channelId`` is the
    more specific field and must not be displaced by it."""
    parsed = _parse_delivery_overrides(
        {"channelName": "slack", "channelId": "C123", "to": "ignored"}
    )

    assert parsed is not None
    assert parsed["channel_id"] == "C123"


@pytest.mark.parametrize("empty", ["", None])
def test_an_empty_channel_id_falls_through_to_to(empty: str | None) -> None:
    """``channelId: ""`` is what a form posts for an untouched field, so it must
    not shadow a recipient the caller did supply under ``to``."""
    parsed = _parse_delivery_overrides({"channelName": "discord", "channelId": empty, "to": "99"})

    assert parsed is not None
    assert parsed["channel_id"] == "99"


def test_missing_recipient_is_still_empty_not_none() -> None:
    """``DeliveryConfig.channel_id`` is a str field; None would break the wire
    round-trip rather than simply meaning "no recipient"."""
    parsed = _parse_delivery_overrides({"channelName": "telegram"})

    assert parsed is not None
    assert parsed["channel_id"] == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [({"bestEffort": True}, True), ({"bestEffort": False}, False), ({}, False)],
)
def test_best_effort_is_carried_from_the_request(raw: dict, expected: bool) -> None:
    parsed = _parse_delivery_overrides({"channelName": "telegram", **raw})

    assert parsed is not None
    assert parsed["best_effort"] is expected


def test_a_block_without_a_channel_name_is_not_channel_delivery() -> None:
    """Unchanged behaviour, pinned: the webhook and none modes reach other
    branches and must not be claimed by the channel parser."""
    assert _parse_delivery_overrides({"mode": "webhook", "webhookUrl": "https://x"}) is None
    assert _parse_delivery_overrides({"mode": "none"}) is None
    assert _parse_delivery_overrides(None) is None
    assert _parse_delivery_overrides("channel") is None


# ── infer_delivery: the override must survive the trip ──────────────────────


@pytest.mark.asyncio
async def test_infer_delivery_keeps_the_recipient_and_flag() -> None:
    overrides = _parse_delivery_overrides(
        {"channelName": "telegram", "to": "1245463966", "bestEffort": True}
    )

    config = await infer_delivery(
        session_storage=None, session_key="agent:main:cli:x", user_overrides=overrides
    )

    assert config.mode == DeliveryMode.CHANNEL
    assert config.channel_name == "telegram"
    assert config.channel_id == "1245463966"
    assert config.best_effort is True


@pytest.mark.asyncio
async def test_infer_delivery_defaults_best_effort_to_strict() -> None:
    """Absent means strict. A silent flip to best-effort would swallow real
    delivery failures, which is the opposite mistake."""
    overrides = _parse_delivery_overrides({"channelName": "telegram", "to": "1"})

    config = await infer_delivery(
        session_storage=None, session_key="agent:main:cli:x", user_overrides=overrides
    )

    assert config.best_effort is False


# ── cron.update ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_keeps_the_recipient_sent_as_to() -> None:
    scheduler = _FakeScheduler(_job(DeliveryConfig(ws_topic="topic-1")))

    await _update(scheduler, {"channelName": "telegram", "to": "1245463966"})

    delivery = scheduler.updated["delivery"]
    assert delivery.channel_id == "1245463966"
    assert delivery.ws_topic == "topic-1", "the existing ws_topic must survive the patch"


@pytest.mark.asyncio
async def test_update_keeps_best_effort() -> None:
    scheduler = _FakeScheduler(_job(DeliveryConfig()))

    await _update(scheduler, {"channelName": "telegram", "to": "1", "bestEffort": True})

    assert scheduler.updated["delivery"].best_effort is True


@pytest.mark.asyncio
async def test_update_on_a_job_with_no_delivery_does_not_raise() -> None:
    """``delivery=None`` is a real stored state, and every branch of the update
    read through it. Patching such a job raised AttributeError."""
    scheduler = _FakeScheduler(_job(None))

    await _update(scheduler, {"channelName": "telegram", "to": "1245463966"})

    delivery = scheduler.updated["delivery"]
    assert delivery.mode == DeliveryMode.CHANNEL
    assert delivery.channel_id == "1245463966"
    assert delivery.ws_topic == ""


@pytest.mark.asyncio
async def test_update_to_webhook_on_a_job_with_no_delivery_does_not_raise() -> None:
    """The webhook branch reads `current_job.delivery.ws_topic` on the same
    line of reasoning, so it fails the same way."""
    scheduler = _FakeScheduler(_job(None))

    await _update(scheduler, {"mode": "webhook", "webhookUrl": "https://example.test/hook"})

    assert scheduler.updated["delivery"].webhook_url == "https://example.test/hook"


@pytest.mark.asyncio
async def test_failure_destination_patch_on_a_job_with_no_delivery_does_not_raise() -> None:
    """Third reader of the same field: the standalone failure-destination patch."""
    scheduler = _FakeScheduler(_job(None))

    await _update(
        scheduler,
        {"failureDestination": {"mode": "channel", "channelName": "slack", "channelId": "C1"}},
    )

    assert scheduler.updated["delivery"].failure_destination is not None


@pytest.mark.asyncio
async def test_update_to_none_still_clears_delivery() -> None:
    """Unchanged behaviour, pinned alongside the new guard."""
    scheduler = _FakeScheduler(_job(DeliveryConfig(channel_id="old", best_effort=True)))

    await _update(scheduler, {"mode": "none"})

    delivery = scheduler.updated["delivery"]
    assert delivery.channel_id == ""
    assert delivery.best_effort is False
