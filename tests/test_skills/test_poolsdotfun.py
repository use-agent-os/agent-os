import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "poolsdotfun-token-launcher" / "scripts"
)

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from poolsfun.fmt import parse_args  # noqa: E402


def test_parse_args_boolean_flag_does_not_consume_subcommand():
    args = parse_args(["--json", "launch", "--name", "TestToken"])
    assert args.get("json") is True
    assert args.get("_") == ["launch"]
    assert args.get("name") == "TestToken"


def test_parse_args_multiple_boolean_flags_before_subcommand():
    args = parse_args(["--debug", "--broadcast", "preflight", "--chain", "monad"])
    assert args.get("debug") is True
    assert args.get("broadcast") is True
    assert args.get("_") == ["preflight"]
    assert args.get("chain") == "monad"


def test_parse_args_boolean_flag_after_subcommand():
    args = parse_args(["assets", "--json"])
    assert args.get("json") is True
    assert args.get("_") == ["assets"]
