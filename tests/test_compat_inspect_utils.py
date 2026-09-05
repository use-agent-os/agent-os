from __future__ import annotations

from agentos.compat.inspect_utils import _accepts_keyword_arg, accepts_keyword_arg


def test_accepts_named_keyword_arg():
    def sample_fn(a: int, b: str = "default") -> None:
        pass

    assert accepts_keyword_arg(sample_fn, "a") is True
    assert accepts_keyword_arg(sample_fn, "b") is True
    assert accepts_keyword_arg(sample_fn, "c") is False


def test_accepts_var_keyword_kwargs():
    def sample_kwargs(a: int, **kwargs) -> None:
        pass

    assert accepts_keyword_arg(sample_kwargs, "a") is True
    assert accepts_keyword_arg(sample_kwargs, "anything") is True
    assert _accepts_keyword_arg(sample_kwargs, "anything") is True


def test_handles_non_callables_and_builtin_objects_gracefully():
    assert accepts_keyword_arg(object(), "foo") is False
    assert accepts_keyword_arg(None, "foo") is False  # type: ignore[arg-type]
    assert accepts_keyword_arg("string", "foo") is False  # type: ignore[arg-type]


def test_positional_only_args():
    def pos_only(a, /, b):
        pass

    assert accepts_keyword_arg(pos_only, "b") is True
    assert accepts_keyword_arg(pos_only, "a") is False
