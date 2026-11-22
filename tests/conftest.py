from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PYTEST_STATE_ROOT = Path(tempfile.gettempdir()) / f"agentos-pytest-{os.getpid()}"

# Under pytest-xdist every worker is its own process. The pid-based defaults
# below already differ per worker, but CI exports one state/log directory for
# the whole job, and workers sharing it race on the same SQLite files and the
# per-state-dir config approval. Give each worker its own subdirectory.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
if _XDIST_WORKER:
    for _name in ("AGENTOS_STATE_DIR", "AGENTOS_LOG_DIR", "AGENTOS_VAULT_KEK_DIR"):
        _preset = os.environ.get(_name)
        if _preset:
            os.environ[_name] = str(Path(_preset) / _XDIST_WORKER)
    # On Linux an xdist worker starts with COLUMNS=80 / LINES=24 already in
    # its environment. Rich reads COLUMNS once, when the shared CLI console in
    # ``agentos.ui`` is built at collection, and then ignores the width a test
    # passes later, so every CLI table renders at 80 columns. A worker has no
    # terminal; tests that need a width set COLUMNS themselves.
    os.environ.pop("COLUMNS", None)
    os.environ.pop("LINES", None)

os.environ.setdefault("AGENTOS_STATE_DIR", str(_PYTEST_STATE_ROOT / "state"))
os.environ.setdefault("AGENTOS_LOG_DIR", str(_PYTEST_STATE_ROOT / "logs"))
# The vault seals unlock.key with a key kept outside the state root, by
# default under the real home; tests must never write there.
os.environ.setdefault("AGENTOS_VAULT_KEK_DIR", str(_PYTEST_STATE_ROOT / "vault-keys"))
os.environ.setdefault("AGENTOS_TURN_CALL_LOG", "0")
# OpenCAP, Surplus and OpenRouter price lookups refresh their catalogs over the
# network when the cache is cold. Default tests must stay offline; tests that
# exercise the refresh opt back in with monkeypatch.setenv. OpenRouter's
# fallback endpoint for a model with no owner endpoint moves between runs, and
# the cost-aware router reads these prices, so a live lookup made tier choice
# depend on whichever reseller OpenRouter happened to list first.
os.environ.setdefault("AGENTOS_OPENCAP_LIVE_PRICING", "0")
os.environ.setdefault("AGENTOS_SURPLUS_LIVE_PRICING", "0")
os.environ.setdefault("AGENTOS_OPENROUTER_LIVE_PRICING", "0")


import pytest  # noqa: E402

# CI splits the suite with --shard=I/N; see tests/ci_shard.py.
from ci_shard import (  # noqa: E402, F401
    pytest_addoption,
    pytest_collection_modifyitems,
    pytest_configure,
    pytest_sessionfinish,
    pytest_testnodedown,
)


@pytest.fixture(autouse=True)
def _reset_redaction_secret_registry():
    """Keep the process-global secret-value registry (WAL-09) from leaking.

    A vault or env test that registers a password/key would otherwise mask that
    literal in an unrelated test's output (e.g. the trading password
    ``correct horse battery`` masking a passphrase-redaction assertion).
    """
    from agentos.redact import clear_registered_secret_values

    clear_registered_secret_values()
    yield
    clear_registered_secret_values()


@pytest.fixture(autouse=True)
def _restore_process_globals(monkeypatch: pytest.MonkeyPatch):
    """Undo process-wide state that production entry points set and never reset.

    Gateway boot calls ``structlog.configure`` (DEBUG filtering, spliced
    processors) and registers the channel manager for the ``cron`` tool; the
    ACP command configures structlog at WARNING. A test that runs one of those
    leaks it into every later test in the same process, so ``capture_logs``
    sees no INFO lines and cron validation sees a stranger's channels. Which
    tests come later depends on how pytest-xdist schedules them.
    """
    import structlog

    config = structlog.get_config()
    config["processors"] = list(config["processors"])

    control = sys.modules.get("agentos.tools.builtin.control")
    if control is not None:
        monkeypatch.setattr(control, "_channel_manager_ref", control._channel_manager_ref)

    yield

    structlog.configure(**config)


@pytest.fixture(autouse=True)
def _fresh_config_approval():
    """Each test starts with no operator config approval on file.

    The state directory is shared by the whole run, and the approval
    (``gateway.config_approval``) is per state directory: a gateway booted by
    one test would otherwise hold the config of the next test's gateway.
    """
    from agentos.gateway.config_approval import approval_path

    target = approval_path()
    target.unlink(missing_ok=True)
    yield
    target.unlink(missing_ok=True)
