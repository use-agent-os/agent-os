from __future__ import annotations

from pathlib import Path

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, get_runtime, reset_runtime


class FakeApprovalQueue:
    def request(self, namespace: str = "exec.approval", params: dict | None = None) -> str:
        return "approval:test"

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        return False

    def resolve(self, approval_id: str, approved: bool) -> None:
        return None


def test_sandbox_runtime_reset_clears_process_global(tmp_path: Path) -> None:
    runtime = configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False),
        approval_queue=FakeApprovalQueue(),
        workspace=tmp_path,
    )

    assert get_runtime() is runtime

    reset_runtime()

    assert get_runtime() is None
