"""Cron job result delivery — Channel + WS + session forward in parallel."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import structlog

from agentos.scheduler.payloads import SCRIPT_KIND
from agentos.scheduler.types import (
    CronJob,
    DeliveryConfig,
    DeliveryMode,
    FailureDestination,
    SessionTarget,
)
from agentos.session.keys import parse_agent_id

log = structlog.get_logger(__name__)

SCRIPT_HANDLER_KEY = "script_run"


_WEBHOOK_TIMEOUT_SECONDS = 10.0
_REPLY_DIRECTIVE_RE = re.compile(r"\[\[\s*(?:reply_to_current|reply_to\s*:\s*[^\]\n]+)\s*\]\]\s*")


def strip_reply_directives(text: str | None) -> str | None:
    if text is None:
        return None
    return _REPLY_DIRECTIVE_RE.sub("", text).lstrip("\n")


def validate_webhook_url(url: str) -> None:
    """Raise ValueError if ``url`` is not a syntactically valid http(s) URL."""
    if not url:
        raise ValueError("webhook URL is required")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError(f"invalid webhook URL: {url!r}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"webhook URL must use http or https scheme, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"webhook URL is missing a hostname: {url!r}")


# The origin webchat session a job was born in no longer exists. Deliberately
# distinct from both "delivered" and the failure statuses: there was nothing to
# deliver into, and that is not the job's fault. ``_required_delivery_error``
# fails a run on "delivery_failed", "forward_failed", and a "skipped" channel
# under an explicit delivery mode — this status matches none of them, so the run
# stands. Only ever produced for webchat origins, whose session *is* the
# conversation; a real channel chat (telegram, discord, slack) outlives the
# session and never reaches these branches.
ORIGIN_SESSION_GONE = "origin_gone"

# A script job produced output but is bound to no conversation to mirror it
# into — an isolated job scheduled without ``--session-key``. Like
# ``ORIGIN_SESSION_GONE`` this is not a failure (the script ran, and its stdout
# is on the run record), but it is not "skipped" either: something *was*
# produced and had nowhere to go. Naming it separately is what makes an empty
# chat legible in ``agentos cron runs`` instead of looking like a silent run.
NO_SESSION_TARGET = "no_session_target"


class ChannelStatus(str):
    """A channel-delivery status that can carry the reason behind it.

    A plain ``str`` subclass so every existing ``== "delivery_failed"`` keeps
    working, while the one thing an operator actually needs — *why* Telegram
    refused — survives the trip to the run record instead of living only in a
    log line nobody reads.
    """

    detail: str

    def __new__(cls, value: str, detail: str = "") -> ChannelStatus:
        status = super().__new__(cls, value)
        status.detail = detail
        return status


def _failed(detail: str) -> ChannelStatus:
    """``delivery_failed``, with the reason attached."""
    return ChannelStatus("delivery_failed", detail)


def status_detail(status: object) -> str:
    """The reason carried by *status*, if it carries one."""
    return str(getattr(status, "detail", "") or "")


@dataclass
class DeliveryReport:
    """Result of the delivery pipeline."""

    channel_status: str = "skipped"  # "delivered" | "delivery_failed" | "skipped" | "origin_gone"
    ws_status: str = "skipped"  # "delivered" | "no_subscribers" | "skipped"
    session_status: str = "skipped"  # "delivered" | "forward_failed" | "skipped" | "origin_gone"
    #: Why channel delivery failed, when it did. Empty otherwise.
    channel_detail: str = ""


class DeliveryChain:
    """Three-stage parallel delivery: Channel + WS + session forward.

    DB persistence is NOT part of this chain — the scheduler's existing
    timer.py -> save_execution() pipeline owns execution records.
    """

    def __init__(
        self,
        channel_manager_ref: Callable[[], Any] | None = None,
        ws_emitter: Callable[[str, str, dict], Awaitable[int]] | None = None,
        session_forwarder: Callable[..., Awaitable[bool | None]] | None = None,
    ) -> None:
        self._channel_manager_ref = channel_manager_ref
        self._ws_emitter = ws_emitter
        self._session_forwarder = session_forwarder

    @staticmethod
    def _is_script_job(job: CronJob) -> bool:
        """Whether this job's output came from a script rather than a model turn.

        Several skips in this chain mean "the run already wrote this into the
        session, so mirroring it would duplicate it" — true for an agent turn,
        which appends its own reply to the transcript it ran in. A script job
        has no turn: nothing writes its stdout anywhere unless this chain does,
        so those skips silently discard the output instead of deduplicating it.
        """
        if job.handler_key == SCRIPT_HANDLER_KEY:
            return True
        payload = job.payload if isinstance(job.payload, dict) else {}
        return str(payload.get("kind", "")) == SCRIPT_KIND

    @staticmethod
    def _script_mirror_session_key(job: CronJob, session_key: str) -> str:
        """Where a script job's stdout should be mirrored, or "" if nowhere.

        The originating chat wins — that is the conversation the operator was
        in when they scheduled the job. Failing that, an explicitly bound
        session (``sessionTarget`` of ``session``/``current``) is a destination
        the operator chose. An isolated job created with neither — the shape
        ``agentos cron add --script`` produces without ``--session-key`` — has
        no conversation to mirror into, and says so rather than pretending.
        """
        if job.origin_session_key:
            return job.origin_session_key
        target = (
            job.session_target
            if isinstance(job.session_target, SessionTarget)
            else SessionTarget(job.session_target)
        )
        if target in (SessionTarget.SESSION, SessionTarget.CURRENT):
            return job.session_key or session_key
        return ""

    @staticmethod
    def _is_same_webchat_session_delivery(
        job: CronJob,
        *,
        channel_name: str,
        channel_id: str,
        session_key: str,
    ) -> bool:
        target = (
            job.session_target
            if isinstance(job.session_target, SessionTarget)
            else SessionTarget(job.session_target)
        )
        if target != SessionTarget.CURRENT:
            return False
        if job.origin_session_key and job.origin_session_key != session_key:
            return False
        if job.session_key and job.session_key != session_key:
            return False
        return channel_name == "webchat" and channel_id == f"webchat:{session_key}"

    @staticmethod
    def _is_origin_webchat_session_delivery(
        job: CronJob,
        *,
        channel_name: str,
        channel_id: str,
        session_key: str,
    ) -> bool:
        target = (
            job.session_target
            if isinstance(job.session_target, SessionTarget)
            else SessionTarget(job.session_target)
        )
        if target == SessionTarget.MAIN:
            return False
        if not job.origin_session_key or job.origin_session_key == session_key:
            return False
        return channel_name == "webchat" and channel_id == f"webchat:{job.origin_session_key}"

    async def deliver(
        self,
        job: CronJob,
        result_text: str,
        success: bool,
        summary: str | None,
        session_key: str,
        route_envelope: Any | None = None,
    ) -> DeliveryReport:
        envelope = route_envelope or build_reply_rendezvous_envelope(job, session_key)
        result_text = strip_reply_directives(result_text) or ""
        summary = strip_reply_directives(summary)
        ch_coro = self._deliver_channel(job, result_text, envelope, session_key)
        ws_coro = self._notify_ws(
            job,
            success=success,
            summary=summary,
            session_key=session_key,
        )
        fwd_coro = self._forward_to_session(job, result_text, session_key)
        ch_result, ws_result, fwd_result = await asyncio.gather(
            ch_coro,
            ws_coro,
            fwd_coro,
            return_exceptions=True,
        )

        report = DeliveryReport()
        if isinstance(ch_result, str):
            report.channel_status = ch_result
            report.channel_detail = status_detail(ch_result)
        else:
            # An exception escaped the channel leg entirely — gather turned it
            # into a value. Without this the run would report "delivery failed"
            # with no trace of what raised, anywhere.
            report.channel_status = "delivery_failed"
            report.channel_detail = f"{type(ch_result).__name__}: {ch_result}"
            log.warning(
                "delivery.channel_raised",
                job_id=job.id,
                error_type=type(ch_result).__name__,
                error=str(ch_result),
            )
        report.ws_status = ws_result if isinstance(ws_result, str) else "skipped"
        report.session_status = fwd_result if isinstance(fwd_result, str) else "forward_failed"
        return report

    async def notify_start(self, job: CronJob, task: str) -> None:
        """Emit cron.run.start event (pre-execution, best-effort)."""
        if not self._ws_emitter:
            return
        topic = job.delivery.ws_topic or f"cron:{job.id}"
        try:
            await self._ws_emitter(
                topic,
                "cron.run.start",
                {"jobId": job.id, "jobName": job.name, "task": task[:200]},
            )
        except Exception:
            pass

    async def _deliver_channel(
        self,
        job: CronJob,
        text: str,
        route_envelope: Any,
        session_key: str,
    ) -> str:
        """Deliver the run output to the primary target (success or failure).

        ``failure_destination`` is dispatched separately by
        :func:`scheduler.jobs.execute_with_timeout` so all failure paths
        (agent_run, system_event, timeout, generic exception) reach the FD
        uniformly — not just runs that flow through this method.
        """
        if job.delivery.mode == DeliveryMode.WEBHOOK:
            return await self._deliver_webhook(job, text)
        target = route_envelope.reply_target
        if job.delivery.mode == DeliveryMode.NONE and not (
            target is not None and target.kind == "channel"
        ):
            return "skipped"
        channel_name = (
            target.channel_name
            if target is not None and target.kind == "channel" and target.channel_name
            else job.delivery.channel_name
        )
        channel_id = (
            target.to
            if target is not None and target.kind == "channel" and target.to is not None
            else job.delivery.channel_id
        )
        thread_id = (
            target.thread_id
            if target is not None and target.kind == "channel"
            else job.delivery.thread_id
        )
        if self._is_same_webchat_session_delivery(
            job,
            channel_name=channel_name or "",
            channel_id=channel_id or "",
            session_key=session_key,
        ):
            # An agent turn ran *in* this session and already appended its reply
            # there, so "same session" means the work is done. A script job left
            # nothing behind — mirror its stdout or it reaches no one.
            if self._is_script_job(job):
                return await self._mirror_to_session(
                    job,
                    text,
                    session_key=session_key,
                    target_session_key=session_key,
                )
            return "delivered"
        if self._is_origin_webchat_session_delivery(
            job,
            channel_name=channel_name or "",
            channel_id=channel_id or "",
            session_key=session_key,
        ):
            return await self._deliver_origin_webchat_to_session(job, text, session_key)
        account_id = (
            target.account_id
            if target is not None and target.kind == "channel" and target.account_id
            else job.delivery.account_id
        )
        if not self._channel_manager_ref:
            return "skipped"
        return await self._post_to_channel(
            job_id=job.id,
            text=text,
            channel_name=channel_name,
            channel_id=channel_id,
            account_id=account_id,
            thread_id=thread_id,
        )

    async def _deliver_origin_webchat_to_session(
        self,
        job: CronJob,
        text: str,
        session_key: str,
    ) -> str:
        return await self._mirror_to_session(
            job,
            text,
            session_key=session_key,
            target_session_key=job.origin_session_key,
        )

    async def _mirror_to_session(
        self,
        job: CronJob,
        text: str,
        *,
        session_key: str,
        target_session_key: str,
    ) -> str:
        """Append ``text`` to ``target_session_key``'s transcript.

        Shared by the two channel-side paths that resolve to a chat rather than
        a real channel adapter. Speaks the channel vocabulary
        (``delivered``/``delivery_failed``/``origin_gone``) because
        :func:`handlers._required_delivery_error` reads it as primary delivery.
        """
        text = strip_reply_directives(text) or ""
        if not self._session_forwarder:
            return _failed("no session forwarder is wired into the scheduler")
        if not text or not text.strip():
            return _failed("nothing to mirror: the output was empty")
        if not target_session_key:
            return _failed("no session to mirror into")
        try:
            forwarded = await self._session_forwarder(
                origin_session_key=target_session_key,
                text=text,
                provenance={
                    "kind": "cron",
                    "source_session_key": session_key,
                    "source_tool": f"cron:{job.id}",
                },
            )
        except Exception:
            log.warning(
                "delivery.webchat_forward_failed",
                job_id=job.id,
                origin_session_key=target_session_key,
                exc_info=True,
            )
            return _failed(f"forwarding into {target_session_key} raised")
        # ``False`` means the origin webchat session is gone. This target was
        # *inferred* from the ambient turn, not chosen by the operator: the cron
        # tool synthesises ``mode=ORIGIN`` + ``channel=webchat`` from the live
        # ToolContext (tools/builtin/control.py:424-445) whenever the agent
        # schedules something from a webchat turn. So a vanished origin is the
        # same "nothing to mirror into" as the mode=NONE path, not an operator
        # misconfiguration. Forwarders returning ``None`` stay "delivered".
        if forwarded is False:
            log.info(
                "delivery.webchat_origin_session_missing",
                job_id=job.id,
                origin_session_key=target_session_key,
            )
            return ORIGIN_SESSION_GONE
        return "delivered"

    async def _post_to_channel(
        self,
        *,
        job_id: str,
        text: str,
        channel_name: str,
        channel_id: str,
        account_id: str = "",
        thread_id: str = "",
    ) -> str:
        """Send ``text`` via the registered channel adapter for ``channel_name``."""
        text = strip_reply_directives(text) or ""
        if not self._channel_manager_ref:
            return "skipped"
        cm = self._channel_manager_ref()
        if cm is None:
            return "skipped"

        if hasattr(cm, "resolve_delivery_target"):
            resolved = cm.resolve_delivery_target(
                target=channel_name or "",
                to=channel_id or "",
                account_id=account_id or "",
                thread_id=thread_id or "",
            )
            if not resolved.ok:
                log.warning(
                    "delivery.resolution_failed",
                    job_id=job_id,
                    channel=channel_name,
                    reason=resolved.reason,
                )
                return _failed(f"delivery target resolution failed: {resolved.reason}")
            adapter = resolved.adapter
            channel_name = resolved.channel_type
            channel_id = resolved.to
            thread_id = resolved.thread_id
        else:
            adapter = cm.get(channel_name)

        if adapter is None:
            log.warning(
                "delivery.adapter_not_found",
                job_id=job_id,
                channel=channel_name,
            )
            return _failed(f"no adapter is registered for channel {channel_name!r}")
        try:
            from agentos.channels.types import OutgoingMessage

            if channel_name == "slack":
                if thread_id:
                    msg = OutgoingMessage(
                        content=text,
                        reply_to=thread_id,
                        metadata={"channel": channel_id},
                    )
                else:
                    msg = OutgoingMessage(
                        content=text,
                        reply_to="cron",
                        metadata={"channel": channel_id, "thread_ts": None},
                    )
            else:
                msg = OutgoingMessage(content=text, reply_to=channel_id or None)
            await asyncio.wait_for(adapter.send(msg), timeout=30.0)
            log.info("delivery.channel_sent", job_id=job_id, channel=channel_name)
            return "delivered"
        except Exception as exc:
            log.warning("delivery.channel_failed", job_id=job_id, exc_info=True)
            # The provider's own words ("Bad Request: chat not found") are what
            # make the run record actionable, so they go on the report too.
            return _failed(str(exc) or type(exc).__name__)

    async def _post_to_webhook(
        self,
        *,
        job_id: str,
        job_name: str,
        text: str,
        url: str,
        token: str,
    ) -> str:
        """POST the finished-run payload to ``url`` with optional bearer ``token``."""
        text = strip_reply_directives(text) or ""
        if not url:
            log.warning("delivery.webhook_url_missing", job_id=job_id)
            return _failed("no webhook URL is configured")
        try:
            validate_webhook_url(url)
        except ValueError as exc:
            log.warning("delivery.webhook_url_invalid", job_id=job_id, reason=str(exc))
            return _failed(f"invalid webhook URL: {exc}")

        import httpx

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "jobId": job_id,
            "jobName": job_name,
            "summary": text,
            "deliveredAt": datetime.now(UTC).isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
            log.info("delivery.webhook_sent", job_id=job_id)
            return "delivered"
        except Exception as exc:
            log.warning("delivery.webhook_failed", job_id=job_id, exc_info=True)
            return _failed(str(exc) or type(exc).__name__)

    async def _deliver_webhook(self, job: CronJob, text: str) -> str:
        """Primary webhook delivery — POST to ``delivery.webhook_url``."""
        return await self._post_to_webhook(
            job_id=job.id,
            job_name=job.name,
            text=text,
            url=job.delivery.webhook_url,
            token=job.delivery.webhook_token,
        )

    async def dispatch_failure_alert(self, job: CronJob, text: str) -> str:
        """Public entry point for dispatching a failed-run alert.

        Called by :func:`scheduler.jobs.execute_with_timeout` for any handler
        failure (agent_run raise, system_event raise, TimeoutError, generic
        Exception) when ``job.delivery.failure_destination`` is configured.
        Returns the same status strings as primary delivery.
        """
        fd = job.delivery.failure_destination
        if fd is None:
            return "skipped"
        return await self._deliver_to_failure_destination(job, text, fd)

    async def _deliver_to_failure_destination(
        self,
        job: CronJob,
        text: str,
        fd: FailureDestination,
    ) -> str:
        """Route a failed-run notification to the configured FailureDestination."""
        if fd.mode == DeliveryMode.WEBHOOK:
            return await self._post_to_webhook(
                job_id=job.id,
                job_name=job.name,
                text=text,
                url=fd.webhook_url,
                token=fd.webhook_token,
            )
        if fd.mode == DeliveryMode.CHANNEL and fd.channel_name:
            return await self._post_to_channel(
                job_id=job.id,
                text=text,
                channel_name=fd.channel_name,
                channel_id=fd.channel_id,
                thread_id=fd.thread_id,
            )
        return "skipped"

    async def _notify_ws(
        self,
        job: CronJob,
        success: bool,
        summary: str | None,
        session_key: str,
    ) -> str:
        if not self._ws_emitter:
            return "skipped"
        summary = strip_reply_directives(summary)
        topic = job.delivery.ws_topic or f"cron:{job.id}"
        payload = {
            "jobId": job.id,
            "jobName": job.name,
            "success": success,
            "summary": summary,
            "sessionKey": session_key,
            "finishedAt": datetime.now(UTC).isoformat(),
        }
        try:
            n = await self._ws_emitter(topic, "cron.run.finished", payload)
            return "delivered" if n > 0 else "no_subscribers"
        except Exception:
            log.warning("delivery.ws_failed", job_id=job.id, exc_info=True)
            return "skipped"

    async def _forward_to_session(
        self,
        job: CronJob,
        text: str,
        session_key: str,
    ) -> str:
        text = strip_reply_directives(text) or ""
        target = (
            job.session_target
            if isinstance(job.session_target, SessionTarget)
            else SessionTarget(job.session_target)
        )
        if not self._session_forwarder:
            return "skipped"
        if job.delivery.mode != DeliveryMode.NONE:
            return "skipped"
        if target == SessionTarget.MAIN:
            return "skipped"
        if not text or not text.strip():
            return "skipped"
        # A script job has no turn of its own, so neither "the run wrote this
        # into its own session" nor "there is no origin" is a reason to drop the
        # output — both just mean this mirror is the only writer. It still needs
        # somewhere to write; an isolated script job bound to no chat has not.
        if self._is_script_job(job):
            mirror_session_key = self._script_mirror_session_key(job, session_key)
            if not mirror_session_key:
                log.info(
                    "delivery.script_output_has_no_session",
                    job_id=job.id,
                    session_target=str(target.value),
                    hint="bind a chat with --session-key to mirror this output",
                )
                return NO_SESSION_TARGET
        else:
            if not job.origin_session_key:
                return "skipped"
            if job.origin_session_key == session_key:
                return "skipped"
            mirror_session_key = job.origin_session_key
        try:
            forwarded = await self._session_forwarder(
                origin_session_key=mirror_session_key,
                text=text,
                provenance={
                    "kind": "cron",
                    "source_session_key": session_key,
                    "source_tool": f"cron:{job.id}",
                },
            )
        except Exception:
            log.warning(
                "delivery.session_forward_failed",
                job_id=job.id,
                origin_session_key=mirror_session_key,
                exc_info=True,
            )
            return "forward_failed"
        # ``False`` means the origin session is gone — the user started a new
        # chat after the job was created. This mirror is opportunistic (the run
        # itself happened elsewhere and already succeeded), so a missing origin
        # is "nothing to do", not a failed run. Reporting ``forward_failed``
        # here made every default web-UI reminder fail once the originating
        # chat was replaced. Forwarders returning ``None`` stay "delivered".
        if forwarded is False:
            log.info(
                "delivery.origin_session_missing",
                job_id=job.id,
                origin_session_key=mirror_session_key,
            )
            return ORIGIN_SESSION_GONE
        return "delivered"


def build_reply_rendezvous_envelope(job: CronJob, session_key: str) -> Any:
    from agentos.scheduler.routing import build_cron_route_envelope

    snapshot = getattr(job.delivery, "originating_reply_target", None)
    if snapshot is not None:
        delivery = DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name=snapshot.channel_name,
            channel_id=snapshot.to,
            account_id=snapshot.account_id,
            thread_id=snapshot.thread_id,
            ws_topic=job.delivery.ws_topic,
        )
        log.info("cron.reply_rendezvous", job_id=job.id, source="originating")
        return build_cron_route_envelope(job, session_key=session_key, delivery=delivery)

    log.info("cron.reply_rendezvous", job_id=job.id, source="last_channel_fallback")
    return build_cron_route_envelope(job, session_key=session_key)


async def infer_delivery(
    session_storage: Any,
    session_key: str,
    user_overrides: dict | None,
) -> DeliveryConfig:
    """Infer delivery config from context. Read-only — no session side effects.

    Priority: user override > session last_channel > NONE.
    Uses session_storage.get_session() (read-only SELECT), NOT session_manager.resume().
    """

    # Priority 1: User explicit override -> mode=CHANNEL
    if user_overrides and user_overrides.get("channel_name"):
        return DeliveryConfig(
            mode=DeliveryMode.CHANNEL,
            channel_name=user_overrides["channel_name"],
            channel_id=user_overrides.get("channel_id", ""),
            account_id=user_overrides.get("account_id", ""),
            thread_id=user_overrides.get("thread_id", ""),
        )

    # Priority 2: Infer from session routing fields -> mode=ORIGIN
    try:
        node = await session_storage.get_session(session_key)
        if node and node.last_channel:
            return DeliveryConfig(
                mode=DeliveryMode.ORIGIN,
                channel_name=node.last_channel,
                channel_id=node.last_to or "",
                account_id=node.last_account_id or "",
                thread_id=node.last_thread_id or "",
            )
    except Exception:
        pass  # session lookup failure -> fall through to NONE

    # Priority 2b: Main-session heartbeat fallback -> most recently updated
    # same-agent session that still carries an outbound routing target.
    if session_key.endswith(":main") and hasattr(session_storage, "list_sessions"):
        try:
            agent_id = parse_agent_id(session_key)
            sessions = await session_storage.list_sessions(agent_id=agent_id, limit=50, offset=0)
            for candidate in sessions:
                if (
                    getattr(candidate, "last_channel", None)
                    and getattr(candidate, "last_to", None)
                    and getattr(candidate, "session_key", None) != session_key
                ):
                    return DeliveryConfig(
                        mode=DeliveryMode.ORIGIN,
                        channel_name=candidate.last_channel,
                        channel_id=candidate.last_to or "",
                        account_id=getattr(candidate, "last_account_id", "") or "",
                        thread_id=getattr(candidate, "last_thread_id", "") or "",
                    )
        except Exception:
            pass

    # Priority 3: No channel context
    return DeliveryConfig(mode=DeliveryMode.NONE)
