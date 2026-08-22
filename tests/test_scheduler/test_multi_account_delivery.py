from __future__ import annotations

from typing import Any

import pytest

from agentos.channels.types import DeliveryTargetResolution, OutgoingMessage
from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.payloads import make_script_payload
from agentos.scheduler.types import CronJob, DeliveryConfig, DeliveryMode, SessionTarget


class _FakeAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.messages: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.messages.append(message)


class _FakeMultiAccountChannelManager:
    def __init__(self, channels: dict[str, Any], channel_types: dict[str, str]) -> None:
        self._channels = channels
        self._channel_types = channel_types

    def resolve_delivery_target(
        self,
        *,
        target: str,
        to: str = "",
        account_id: str = "",
        thread_id: str = "",
    ) -> DeliveryTargetResolution:
        target_name = target.strip()
        target_type = target_name.lower()
        account = account_id.strip()
        to = to.strip()
        thread = thread_id.strip()

        candidates = [
            name
            for name, channel_type in self._channel_types.items()
            if channel_type.lower() == target_type
        ]
        if account:
            if account not in candidates:
                return DeliveryTargetResolution(ok=False, reason="unsupported_account")
            return DeliveryTargetResolution(
                ok=True,
                adapter=self._channels.get(account),
                adapter_name=account,
                channel_type=target_type,
                to=to,
                account_id=account,
                thread_id=thread,
            )

        if target_name in self._channels:
            return DeliveryTargetResolution(
                ok=True,
                adapter=self._channels.get(target_name),
                adapter_name=target_name,
                channel_type=self._channel_types.get(target_name, target_name).lower(),
                to=to,
                account_id=account,
                thread_id=thread,
            )

        if not candidates:
            return DeliveryTargetResolution(ok=False, reason="unsupported_target")
        if len(candidates) > 1:
            return DeliveryTargetResolution(ok=False, reason="ambiguous_account")

        return DeliveryTargetResolution(
            ok=True,
            adapter=self._channels.get(candidates[0]),
            adapter_name=candidates[0],
            channel_type=target_type,
            to=to,
            account_id=account,
            thread_id=thread,
        )


@pytest.mark.asyncio
async def test_cron_delivery_resolves_and_honors_account_id() -> None:
    # Setup two Slack adapters
    adapter1 = _FakeAdapter("slack-bot-1")
    adapter2 = _FakeAdapter("slack-bot-2")
    channels = {"slack-bot-1": adapter1, "slack-bot-2": adapter2}
    channel_types = {"slack-bot-1": "slack", "slack-bot-2": "slack"}

    manager = _FakeMultiAccountChannelManager(channels, channel_types)
    chain = DeliveryChain(channel_manager_ref=lambda: manager)

    job = CronJob(
        id="job-1",
        name="multi-acc-test",
        handler_key="script_run",
        payload=make_script_payload("test.sh"),
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="slack",
            channel_id="C0123ABCDEF",
            account_id="slack-bot-1",
        ),
    )

    report = await chain.deliver(
        job,
        result_text="hello world",
        success=True,
        summary="hello world",
        session_key="cron:job-1:run:deadbeef",
    )

    assert report.channel_status == "delivered"
    # Ensure message went to adapter 1 and not adapter 2
    assert len(adapter1.messages) == 1
    assert adapter1.messages[0].content == "hello world"
    assert len(adapter2.messages) == 0


@pytest.mark.asyncio
async def test_cron_delivery_resolution_failure_reports_failed() -> None:
    # Setup one Slack adapter
    adapter = _FakeAdapter("slack-bot-1")
    channels = {"slack-bot-1": adapter}
    channel_types = {"slack-bot-1": "slack"}

    manager = _FakeMultiAccountChannelManager(channels, channel_types)
    chain = DeliveryChain(channel_manager_ref=lambda: manager)

    # Use a non-existent account_id
    job = CronJob(
        id="job-1",
        name="multi-acc-test",
        handler_key="script_run",
        payload=make_script_payload("test.sh"),
        session_target=SessionTarget.ISOLATED,
        delivery=DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name="slack",
            channel_id="C0123ABCDEF",
            account_id="slack-bot-nonexistent",
        ),
    )

    report = await chain.deliver(
        job,
        result_text="hello world",
        success=True,
        summary="hello world",
        session_key="cron:job-1:run:deadbeef",
    )

    assert report.channel_status == "delivery_failed"
    assert "delivery target resolution failed: unsupported_account" in report.channel_detail
