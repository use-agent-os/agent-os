"""Version-skew policy for CLI commands that talk to the running gateway.

The #1 documented post-upgrade regret across the case studies is SILENT VERSION
SKEW: the package upgrades, the daemon keeps running old code, the command
reports success, and the operator then debugs phantom config bugs. This module
makes skew loud.

Two directions, asymmetric on purpose:

* **Gateway OLDER than CLI** (the typical post-upgrade state): warn on stderr,
  never block. The operator just needs to restart the gateway.
* **Gateway NEWER than CLI** (operator downgraded the CLI, or is driving a
  newer gateway from a stale environment): REFUSE. A newer gateway may have
  written config with newer schema; an older CLI acting on it risks
  corruption. An ``AGENTOS_ALLOW_VERSION_SKEW=1`` escape hatch exists for
  emergencies.

Throttled to a single warning line per invocation via ``SkewReporter``.
"""

from __future__ import annotations

import os
import sys

from agentos.cli.version_utils import compare_versions

ALLOW_SKEW_ENV = "AGENTOS_ALLOW_VERSION_SKEW"


class VersionSkewError(Exception):
    """Raised when the gateway is NEWER than the CLI and skew is not allowed."""


def _skew_allowed() -> bool:
    return os.environ.get(ALLOW_SKEW_ENV, "").strip() == "1"


def evaluate_skew(*, cli_version: str, gateway_version: str | None) -> str | None:
    """Classify the CLI/gateway version relationship.

    Returns one of ``None`` (equal / unknown), ``"gateway_older"``, or
    ``"gateway_newer"``.
    """

    if not gateway_version:
        return None
    cmp = compare_versions(gateway_version, cli_version)
    if cmp < 0:
        return "gateway_older"
    if cmp > 0:
        return "gateway_newer"
    return None


class SkewReporter:
    """Emits at most one skew warning per invocation; enforces the refusal."""

    def __init__(self) -> None:
        self._warned = False

    def check(
        self,
        *,
        cli_version: str,
        gateway_version: str | None,
    ) -> None:
        """Warn or refuse based on the skew direction.

        Raises :class:`VersionSkewError` when the gateway is newer and the
        escape hatch is not set. Warns to stderr (once) when the gateway is
        older.
        """

        state = evaluate_skew(cli_version=cli_version, gateway_version=gateway_version)
        if state == "gateway_older":
            if self._warned:
                return
            self._warned = True
            print(
                f"⚠ Gateway is running an OLDER version ({gateway_version}) than the "
                f"CLI ({cli_version}). Run 'agentos gateway restart' to apply the "
                f"upgrade.",
                file=sys.stderr,
            )
            return
        if state == "gateway_newer":
            if _skew_allowed():
                if not self._warned:
                    self._warned = True
                    print(
                        f"⚠ Gateway ({gateway_version}) is NEWER than the CLI "
                        f"({cli_version}); proceeding because {ALLOW_SKEW_ENV}=1.",
                        file=sys.stderr,
                    )
                return
            raise VersionSkewError(
                f"Gateway ({gateway_version}) is NEWER than this CLI ({cli_version}). "
                "The gateway may have written config with a newer schema, so this "
                "older CLI refuses to act on it. Upgrade the CLI (agentos upgrade) "
                "or restart the gateway from this environment. To override in an "
                f"emergency, set {ALLOW_SKEW_ENV}=1."
            )
