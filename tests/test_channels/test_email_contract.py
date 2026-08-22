from __future__ import annotations

from agentos.channels import email
from agentos.channels.contract import run_channel_contract


def test_email_channel_contract() -> None:
    run_channel_contract(email)
