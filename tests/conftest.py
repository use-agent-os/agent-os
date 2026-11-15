from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PYTEST_STATE_ROOT = Path(tempfile.gettempdir()) / f"agentos-pytest-{os.getpid()}"

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
