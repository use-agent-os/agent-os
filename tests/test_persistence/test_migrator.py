from __future__ import annotations

import builtins
import contextlib
import warnings
from pathlib import Path
from types import SimpleNamespace

from agentos.persistence import migrator
from agentos.persistence.migrator import apply_pending


def test_apply_pending_registers_python312_datetime_adapter(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "V001__demo.py").write_text(
        "from yoyo import step\n"
        "__depends__ = set()\n"
        "steps = [step('CREATE TABLE demo (id INTEGER PRIMARY KEY)')]\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        applied = apply_pending(str(tmp_path / "demo.sqlite"), migrations_dir)

    assert applied == ["V001__demo"]


def test_apply_pending_does_not_reapply_a_migration_a_sibling_process_just_landed(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression test: to_apply() and apply_migrations() must run under the
    same lock acquisition.

    backend.lock() only serializes what happens *inside* it -- it does not
    retroactively invalidate a ``pending`` list computed before the lock was
    even requested. If two processes boot concurrently against the same
    database (multiple workers, a rolling restart sharing a volume), both
    can compute the same stale ``pending`` list before either acquires the
    lock; the second one to get the lock would then blindly try to
    re-apply what the first already committed, since yoyo's apply_one()
    does not re-check applied state before running. This simulates that:
    the fake lock() applies a "sibling's" migration as a side effect of
    being entered, standing in for a sibling process that finished its own
    full apply_pending() cycle while we were blocked waiting for the lock.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "V001__demo.py").write_text(
        "from yoyo import step\n"
        "__depends__ = set()\n"
        "steps = [step('CREATE TABLE demo (id INTEGER PRIMARY KEY)')]\n",
        encoding="utf-8",
    )

    class RacingBackend:
        def __init__(self) -> None:
            self.applied_ids: set[str] = set()
            self.to_apply_calls = 0
            self.apply_migrations_calls: list[list[str]] = []

        def to_apply(self, migrations):
            self.to_apply_calls += 1
            return [m for m in migrations if m.id not in self.applied_ids]

        @contextlib.contextmanager
        def lock(self):
            # Stand-in for: a sibling process fully completed its own
            # apply_pending() -- read, apply, mark-applied -- while we were
            # blocked waiting to acquire this same lock.
            self.applied_ids.add("V001__demo")
            yield

        def apply_migrations(self, pending):
            ids = [m.id for m in pending]
            self.apply_migrations_calls.append(ids)
            for m in pending:
                if m.id in self.applied_ids:
                    raise RuntimeError(
                        f"attempted to re-apply {m.id!r}, already applied by a concurrent process"
                    )
            self.applied_ids.update(ids)

        def close(self) -> None:
            pass

    backend = RacingBackend()
    monkeypatch.setattr(migrator, "get_backend", lambda _url: backend)

    applied = apply_pending(str(tmp_path / "demo.sqlite"), migrations_dir)

    # to_apply() must see the sibling's already-applied migration and
    # correctly exclude it -- not attempt to re-apply it and raise.
    assert applied == []
    assert backend.apply_migrations_calls == []
    assert backend.to_apply_calls == 1


def test_apply_pending_forces_utf8_when_yoyo_loads_python_migrations(
    tmp_path: Path, monkeypatch
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    migration_file = migrations_dir / "V999__utf8.py"
    migration_file.write_text("marker = '— 界'\n", encoding="utf-8")

    real_open = builtins.open
    seen: dict[str, object] = {}

    def legacy_locale_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if "b" not in mode and "encoding" not in kwargs:
            raise UnicodeDecodeError("gbk", b"\x80", 0, 1, "fake legacy locale")
        seen["encoding"] = kwargs.get("encoding")
        return real_open(file, mode, *args, **kwargs)

    def fake_read_migrations(path: str):
        assert path == str(migrations_dir)
        with open(migration_file) as handle:
            seen["content"] = handle.read()
        return [SimpleNamespace(id="V999__utf8")]

    class FakeBackend:
        def to_apply(self, migrations):
            seen["migrations"] = migrations
            return [SimpleNamespace(id="V999__utf8")]

        def lock(self):
            return contextlib.nullcontext()

        def apply_migrations(self, pending):
            seen["pending"] = [item.id for item in pending]

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(migrator.builtins, "open", legacy_locale_open)
    monkeypatch.setattr(migrator, "read_migrations", fake_read_migrations)
    monkeypatch.setattr(migrator, "get_backend", lambda _url: FakeBackend())

    applied = apply_pending(str(tmp_path / "demo.sqlite"), migrations_dir)

    assert applied == ["V999__utf8"]
    assert seen["encoding"] == "utf-8"
    assert seen["content"] == "marker = '— 界'\n"
    assert seen["pending"] == ["V999__utf8"]
    assert seen["closed"] is True
