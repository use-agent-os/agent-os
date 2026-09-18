"""Issue #2203: Ollama dropped companion blocks and ignored images.

Two silent losses in message translation.

``_build_ollama_messages`` emitted a ``role: tool`` message for each tool
result and then ``continue``d, so any text or image sitting in the *same*
message -- "here is the output, now look at this" -- never reached the model.

``_build_ollama_message`` had no branch for ``ContentBlockImage`` at all, so a
vision model (llava, llama3.2-vision, qwen2.5-vl) was sent the prompt with the
picture removed and answered about nothing.

Both failures are invisible from the call site: the request succeeds and the
reply is merely wrong.
"""

from __future__ import annotations

import pytest

from agentos.provider.ollama import _build_ollama_message, _build_ollama_messages
from agentos.provider.types import (
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
JPG_B64 = "dGVzdF9pbWFnZV9ieXRlcw=="


def _image(data: str, *, source_type: str = "base64", media: str = "image/png"):
    return ContentBlockImage(source_type=source_type, media_type=media, data=data)


# ── companion blocks alongside a tool result ────────────────────────────────


def test_text_next_to_a_tool_result_is_not_dropped() -> None:
    """The reported case: the instruction that gave the result its purpose."""
    translated = _build_ollama_messages(
        [
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="call_1", content="result data"),
                    ContentBlockText(text="Please analyze the above result."),
                ],
            )
        ]
    )

    assert [m["role"] for m in translated] == ["tool", "user"]
    assert translated[0]["content"] == "result data"
    assert translated[1]["content"] == "Please analyze the above result."


def test_the_companion_follows_the_result_it_refers_to() -> None:
    """Order carries meaning: "the above result" has to come after it."""
    translated = _build_ollama_messages(
        [
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="c1", content="A"),
                    ContentBlockText(text="explain"),
                ],
            )
        ]
    )

    assert translated[0]["role"] == "tool"
    assert translated[-1]["content"] == "explain"


def test_an_image_next_to_a_tool_result_is_not_dropped() -> None:
    translated = _build_ollama_messages(
        [
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="c1", content="chart.png written"),
                    ContentBlockImage(source_type="base64", media_type="image/png", data=PNG_B64),
                ],
            )
        ]
    )

    assert translated[0]["role"] == "tool"
    assert translated[1]["images"] == [PNG_B64]


def test_several_tool_results_each_get_a_message_and_the_text_still_follows() -> None:
    translated = _build_ollama_messages(
        [
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="c1", content="first"),
                    ContentBlockToolResult(tool_use_id="c2", content="second"),
                    ContentBlockText(text="compare them"),
                ],
            )
        ]
    )

    assert [m["role"] for m in translated] == ["tool", "tool", "user"]
    assert [m["content"] for m in translated] == ["first", "second", "compare them"]


def test_a_tool_result_alone_still_produces_only_a_tool_message() -> None:
    """No companion blocks means no empty trailing message."""
    translated = _build_ollama_messages(
        [Message(role="user", content=[ContentBlockToolResult(tool_use_id="c1", content="x")])]
    )

    assert len(translated) == 1
    assert translated[0]["role"] == "tool"


def test_a_blank_companion_does_not_add_an_empty_message() -> None:
    """An empty text block is not content worth a turn of its own."""
    translated = _build_ollama_messages(
        [
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="c1", content="x"),
                    ContentBlockText(text=""),
                ],
            )
        ]
    )

    assert len(translated) == 1


def test_the_tool_name_pairing_still_survives() -> None:
    """Unchanged behaviour, pinned: the assistant's tool_use names the call so
    the later result can carry `tool_name`, which is what Ollama pairs on."""
    translated = _build_ollama_messages(
        [
            Message(
                role="assistant",
                content=[ContentBlockToolUse(id="c1", name="read_file", input={"path": "a"})],
            ),
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id="c1", content="body"),
                    ContentBlockText(text="summarise"),
                ],
            ),
        ]
    )

    assert translated[1]["tool_name"] == "read_file"
    assert translated[2]["content"] == "summarise"


# ── images ──────────────────────────────────────────────────────────────────


def test_a_base64_image_becomes_an_images_entry() -> None:
    message = _build_ollama_message(
        Message(role="user", content=[ContentBlockText(text="what is this?"), _image(PNG_B64)])
    )

    assert message["content"] == "what is this?"
    assert message["images"] == [PNG_B64]


def test_several_images_keep_their_order() -> None:
    message = _build_ollama_message(
        Message(role="user", content=[_image(PNG_B64), _image(JPG_B64, media="image/jpeg")])
    )

    assert message["images"] == [PNG_B64, JPG_B64]


def test_a_data_url_wrapper_is_stripped() -> None:
    """Blocks arrive either bare or data-URL wrapped depending on the surface
    that built them; Ollama decodes the field as base64 and chokes on the
    ``data:image/png;base64,`` prefix."""
    message = _build_ollama_message(
        Message(role="user", content=[_image(f"data:image/png;base64,{PNG_B64}")])
    )

    assert message["images"] == [PNG_B64]


def test_a_url_image_is_named_in_the_text_not_sent_as_base64() -> None:
    """Ollama's ``images`` array has no URL form. Passing the URL through as if
    it were base64 makes the server fail the decode, and dropping it silently
    is the very failure this change exists to remove -- so it is surfaced in
    the text where the model can at least see it was referenced.
    """
    message = _build_ollama_message(
        Message(
            role="user",
            content=[
                ContentBlockText(text="describe"),
                _image("https://example.test/cat.png", source_type="url"),
            ],
        )
    )

    assert "images" not in message
    assert "https://example.test/cat.png" in message["content"]


def test_a_mix_of_url_and_base64_keeps_only_the_base64_in_images() -> None:
    message = _build_ollama_message(
        Message(
            role="user",
            content=[
                _image(PNG_B64),
                _image("https://example.test/cat.png", source_type="url"),
            ],
        )
    )

    assert message["images"] == [PNG_B64]
    assert "example.test" in message["content"]


def test_a_message_with_no_images_has_no_images_key() -> None:
    """An empty array is not the same as absence; some servers reject it."""
    message = _build_ollama_message(Message(role="user", content=[ContentBlockText(text="plain")]))

    assert "images" not in message


def test_images_ride_alongside_tool_calls() -> None:
    """The two optional keys are independent and must not displace each other."""
    message = _build_ollama_message(
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="looking"),
                _image(PNG_B64),
                ContentBlockToolUse(id="c1", name="zoom", input={"x": 1}),
            ],
        )
    )

    assert message["images"] == [PNG_B64]
    assert message["tool_calls"][0]["function"]["name"] == "zoom"


# ── nothing that worked before changes ──────────────────────────────────────


def test_a_plain_string_message_is_untouched() -> None:
    assert _build_ollama_message(Message(role="user", content="hello")) == {
        "role": "user",
        "content": "hello",
    }


def test_text_blocks_are_still_joined_with_a_space() -> None:
    message = _build_ollama_message(
        Message(role="user", content=[ContentBlockText(text="a"), ContentBlockText(text="b")])
    )

    assert message["content"] == "a b"


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_the_companion_message_keeps_the_original_role(role: str) -> None:
    translated = _build_ollama_messages(
        [
            Message(
                role=role,
                content=[
                    ContentBlockToolResult(tool_use_id="c1", content="r"),
                    ContentBlockText(text="t"),
                ],
            )
        ]
    )

    assert translated[-1]["role"] == role
