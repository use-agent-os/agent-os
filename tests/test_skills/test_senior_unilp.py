import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = (
    ROOT
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "senior-unilp-manager"
    / "scripts"
)

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from unilp.fmt import parse_args  # noqa: E402


def test_parse_args_boolean_flag_does_not_consume_subcommand():
    args = parse_args(["--json", "positions", "--chain", "ethereum"])
    assert args.get("json") is True
    assert args.get("_") == ["positions"]
    assert args.get("chain") == "ethereum"


def test_parse_args_include_v3_flag_before_subcommand():
    args = parse_args(["--include-v3", "pools", "--min-tvl", "1000"])
    assert args.get("include-v3") is True
    assert args.get("_") == ["pools"]
    assert args.get("min-tvl") == "1000"


def test_parse_args_multiple_boolean_flags_before_subcommand():
    args = parse_args(["--all-pools", "--no-hook", "--json", "pools"])
    assert args.get("all-pools") is True
    assert args.get("no-hook") is True
    assert args.get("json") is True
    assert args.get("_") == ["pools"]
