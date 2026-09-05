from __future__ import annotations

from typing import Any

import pytest

from agentos.channels._telegram_formatting import render_telegram_html
from agentos.channels.telegram import TelegramApiError, TelegramChannel, TelegramChannelConfig
from agentos.channels.types import OutgoingMessage


def test_telegram_markdown_renders_bold_code_and_two_column_table() -> None:
    markdown = """Skill dùng `agentos channels list`.

AgentOS có **1 channel**:

| Thông tin | Giá trị |
| --- | --- |
| **Tên** | `telegram-test` |
| **Trạng thái** | ✅ Enabled |
"""

    rendered = render_telegram_html(markdown)

    assert "<code>agentos channels list</code>" in rendered
    assert "AgentOS có <b>1 channel</b>:" in rendered
    assert "<b>Thông tin — Giá trị</b>" in rendered
    assert "<b>Tên:</b> <code>telegram-test</code>" in rendered
    assert "<b>Trạng thái:</b> ✅ Enabled" in rendered
    assert "| --- |" not in rendered
    assert "**" not in rendered
    assert "`" not in rendered


def test_telegram_markdown_escapes_html_and_preserves_code_blocks() -> None:
    markdown = """# Result <safe>

Use **care & caution** with `x < 2`.

```python
if x < 2:
    print("&")
```
"""

    rendered = render_telegram_html(markdown)

    assert "<b>Result &lt;safe&gt;</b>" in rendered
    assert "Use <b>care &amp; caution</b> with <code>x &lt; 2</code>." in rendered
    assert (
        '<pre><code class="language-python">'
        "if x &lt; 2:\n    print(&quot;&amp;&quot;)</code></pre>"
    ) in rendered


def test_telegram_send_payload_auto_renders_html() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="**Ready**: `agentos status`", reply_to="42")
    )

    assert payload == {
        "chat_id": "42",
        "text": "<b>Ready</b>: <code>agentos status</code>",
        "parse_mode": "HTML",
    }


def test_telegram_send_payload_respects_explicit_parse_mode_override() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="*caller-owned*",
            reply_to="42",
            metadata={"parse_mode": "MarkdownV2"},
        )
    )

    assert payload["text"] == "*caller-owned*"
    assert payload["parse_mode"] == "MarkdownV2"


def test_telegram_send_payload_can_explicitly_disable_rendering() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="**literal**", reply_to="42", metadata={"parse_mode": ""})
    )

    assert payload["text"] == "**literal**"
    assert "parse_mode" not in payload


@pytest.mark.asyncio
async def test_telegram_send_falls_back_to_plain_text_on_entity_parse_error() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> dict[str, int]:
        calls.append((method, dict(payload or {})))
        if len(calls) == 1:
            raise TelegramApiError("Bad Request: can't parse entities")
        return {"message_id": 7}

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001

    result = await channel.send(
        OutgoingMessage(content="**Ready**: `agentos status`", reply_to="42")
    )

    assert result == {"message_id": 7}
    assert calls[0][1] == {
        "chat_id": "42",
        "text": "<b>Ready</b>: <code>agentos status</code>",
        "parse_mode": "HTML",
    }
    assert calls[1][1] == {
        "chat_id": "42",
        "text": "**Ready**: `agentos status`",
    }


# ── Ragged / mismatched table rows ─────────────────────────────────────


def test_two_column_table_with_single_cell_row_does_not_crash() -> None:
    """Single-cell rows in a 2-col table pad the missing column."""
    markdown = """| Header A | Header B |
| --- | --- |
| Row 1 Only |
"""
    rendered = render_telegram_html(markdown)
    assert "Header A — Header B" in rendered
    assert "Row 1 Only" in rendered


def test_three_column_table_with_mismatched_row_length_does_not_crash() -> None:
    """Mismatched column count in a 3-col table pads missing cells."""
    markdown = """| H1 | H2 | H3 |
| --- | --- | --- |
| A | B |
| C | D | E | F |
"""
    rendered = render_telegram_html(markdown)
    assert rendered


def test_three_column_table_with_extra_cells_truncated() -> None:
    """Extra cells beyond header count are truncated instead of crashing."""
    markdown = """| Head | Tail |
| --- | --- |
| A | B | C | D |
"""
    rendered = render_telegram_html(markdown)
    assert "Head" in rendered
    assert "Tail" in rendered
    assert "A" in rendered


def test_ragged_rows_preserve_prior_content() -> None:
    """Mismatched rows after valid rows keep prior parsed content."""
    markdown = """| Name | Value |
| --- | --- |
| **OK** | ✅ Pass |
| Orphan |
| **Crit** | ❌ Fail |
"""
    rendered = render_telegram_html(markdown)
    assert "✅ Pass" in rendered
    assert "❌ Fail" in rendered
    assert "Orphan" in rendered
