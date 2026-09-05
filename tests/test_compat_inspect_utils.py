"""Unit tests for agentos.compat.inspect_utils."""

from __future__ import annotations

from agentos.compat.inspect_utils import _accepts_keyword_arg, accepts_keyword_arg


def test_accepts_keyword_arg_explicit_arg() -> None:
    def fn(a: int, b: str, target: bool = False) -> None:
        pass

    assert accepts_keyword_arg(fn, "target") is True
    assert accepts_keyword_arg(fn, "a") is True
    assert accepts_keyword_arg(fn, "missing") is False


def test_accepts_keyword_arg_kwargs() -> None:
    def fn_var(a: int, **kwargs: object) -> None:
        pass

    assert accepts_keyword_arg(fn_var, "any_name") is True
    assert accepts_keyword_arg(fn_var, "target") is True


def test_accepts_keyword_arg_keyword_only() -> None:
    def fn_kwonly(*, target: str) -> None:
        pass

    assert accepts_keyword_arg(fn_kwonly, "target") is True
    assert accepts_keyword_arg(fn_kwonly, "other") is False


def test_accepts_keyword_arg_non_callable_returns_false() -> None:
    assert accepts_keyword_arg(None, "target") is False
    assert accepts_keyword_arg(123, "target") is False
    assert accepts_keyword_arg("string", "target") is False
    assert accepts_keyword_arg({}, "target") is False


def test_accepts_keyword_arg_class_and_method() -> None:
    class MyHandler:
        def handle(self, message: str, *, flush_receipt_status: bool = False) -> None:
            pass

    handler = MyHandler()
    assert accepts_keyword_arg(handler.handle, "flush_receipt_status") is True
    assert accepts_keyword_arg(handler.handle, "unknown") is False


def test_accepts_keyword_arg_private_alias() -> None:
    def sample(token: str) -> None:
        pass

    assert _accepts_keyword_arg(sample, "token") is True
    assert _accepts_keyword_arg(sample, "other") is False
