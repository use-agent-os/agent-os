"""Prompt layout contract for interactive CLI surfaces (issue #86).

Free-text questions must own their line, with the input caret on the line
below and the default offered as a hint instead of being pre-filled into the
buffer (typing must replace the default, not append to it).
"""

from __future__ import annotations

import pytest

from agentos import ui


class _Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value

    def unsafe_ask(self):
        return self.value


class _FakeQuestionary:
    """Records the message/kwargs each prompt was constructed with."""

    def __init__(self, answer=""):
        self.calls: list[tuple[str, str, dict]] = []
        self._answer = answer

    def _record(self, kind):
        def build(message, **kwargs):
            self.calls.append((kind, message, kwargs))
            return _Answer(self._answer)

        return build

    def __getattr__(self, name):
        if name in {"select", "text", "confirm", "password", "checkbox"}:
            return self._record(name)
        raise AttributeError(name)

    Choice = object()


@pytest.fixture
def styled(monkeypatch):
    """Force the styled path; without a Style the wrapper passes through."""

    monkeypatch.setattr(ui, "questionary_style", lambda: object())
    return ui.styled_questionary


def test_prompt_message_puts_caret_on_the_next_line():
    assert ui.prompt_message("Channel name") == f"Channel name\n{ui.PROMPT_CARET}"


def test_prompt_message_offers_a_non_empty_default_as_a_hint():
    assert (
        ui.prompt_message("Channel name", "telegram")
        == f"Channel name (telegram)\n{ui.PROMPT_CARET}"
    )


@pytest.mark.parametrize("default", [None, ""])
def test_prompt_message_omits_the_hint_when_there_is_no_default(default):
    assert ui.prompt_message("Channel name", default) == (f"Channel name\n{ui.PROMPT_CARET}")


@pytest.mark.parametrize("kind", ["text", "password"])
def test_free_text_prompts_are_reformatted(styled, kind):
    fake = _FakeQuestionary()

    getattr(styled(fake), kind)("Bot token")

    assert fake.calls[0][1] == f"Bot token\n{ui.PROMPT_CARET}"


@pytest.mark.parametrize("kind", ["select", "confirm"])
def test_choice_prompts_keep_their_message(styled, kind):
    fake = _FakeQuestionary()

    getattr(styled(fake), kind)("Channel type", choices=["telegram"])

    assert fake.calls[0][1] == "Channel type"


def test_text_default_is_not_prefilled_into_the_buffer(styled):
    fake = _FakeQuestionary()

    styled(fake).text("Channel name", default="telegram")

    _kind, message, kwargs = fake.calls[0]
    assert message == f"Channel name (telegram)\n{ui.PROMPT_CARET}"
    assert "default" not in kwargs


def test_empty_submission_falls_back_to_the_default(styled):
    fake = _FakeQuestionary(answer="")

    answer = styled(fake).text("Channel name", default="telegram").ask()

    assert answer == "telegram"


def test_typed_value_replaces_the_default(styled):
    fake = _FakeQuestionary(answer="mybot")

    answer = styled(fake).text("Channel name", default="telegram").ask()

    assert answer == "mybot"


def test_cancellation_is_not_swallowed_by_the_default(styled):
    """``None`` means Ctrl+C / Esc; callers turn it into UserCancelledError."""

    fake = _FakeQuestionary(answer=None)

    answer = styled(fake).text("Channel name", default="telegram").ask()

    assert answer is None


def test_unsafe_ask_applies_the_same_fallback(styled):
    fake = _FakeQuestionary(answer="")

    answer = styled(fake).text("Channel name", default="telegram").unsafe_ask()

    assert answer == "telegram"


def test_prompt_without_a_default_is_returned_unwrapped(styled):
    fake = _FakeQuestionary(answer="")

    assert styled(fake).text("Model id").ask() == ""


def test_passthrough_when_the_brand_style_is_unavailable(monkeypatch):
    """Test stubs and installs without questionary must keep raw messages."""

    monkeypatch.setattr(ui, "questionary_style", lambda: None)
    fake = _FakeQuestionary()

    assert ui.styled_questionary(fake) is fake
