"""Turn run kinds: the ``run_kind`` label every turn carries.

The entry point that starts a turn names it (``sessions.send`` a
``session_turn``, a channel message a ``channel_turn``, the scheduler a
``cron_turn``, ...), and later stages decide policy from that label --
proactive memory recall, session titling, restart recovery, memory review.
Producers and consumers both import the names from here, so a policy that
keys on a run kind cannot drift from the value production code writes.

A leaf module on purpose: ``agentos.memory`` imports it at module scope and
must not reach ``agentos.session`` or ``agentos.gateway`` that way.
"""

from __future__ import annotations

from typing import Final

#: A turn started without a label: local ``agentos chat`` / ``agentos agent``,
#: ``TaskRuntime.send`` follow-ups (inter-session sends, child-completion
#: wakes), and restart resumes of an unlabelled turn. Not a statement about
#: who wrote the message -- provenance and the prompt mode decide that.
DEFAULT: Final = "default"
#: A message sent through ``sessions.send`` / ``chat.send``: WebChat, the
#: desktop app, ``agentos chat`` against a gateway, the MCP bridge.
SESSION_TURN: Final = "session_turn"
#: An inbound message on a messaging channel (Telegram, Discord, ...).
CHANNEL_TURN: Final = "channel_turn"
#: A scheduled ``agent_run`` cron job.
CRON_TURN: Final = "cron_turn"
#: A heartbeat tick.
HEARTBEAT: Final = "heartbeat"
#: A sub-agent spawned with ``sessions_spawn``.
SUBAGENT: Final = "subagent"
#: An ACP coding-harness child session.
ACP: Final = "acp"
#: The background memory review that follows a user turn.
MEMORY_NUDGE: Final = "memory_nudge"

#: Labels a client may pass as ``runKind`` on ``sessions.send`` / ``chat.send``
#: for a person's own message. Nothing in this tree produces them.
CLIENT_USER_RUN_KINDS: Final = frozenset({"chat", "interactive", "user"})

#: Turns a person typed into a conversation, as labelled by the gateway.
USER_RUN_KINDS: Final = frozenset({SESSION_TURN, CHANNEL_TURN}) | CLIENT_USER_RUN_KINDS

#: Run kinds that may carry a person's message. :data:`DEFAULT` is included
#: because untagged turns are often interactive; whether one actually is
#: comes from its input provenance and prompt mode, not from the label.
INTERACTIVE_RUN_KINDS: Final = USER_RUN_KINDS | {DEFAULT}

#: Turns that run in a child session on behalf of a parent.
CHILD_RUN_KINDS: Final = frozenset({SUBAGENT, ACP})

#: Machine-started turns: nobody typed their message.
BACKGROUND_RUN_KINDS: Final = frozenset({CRON_TURN, HEARTBEAT, MEMORY_NUDGE}) | CHILD_RUN_KINDS
