"""Config fields for the curated memory store."""

from agentos.gateway.config import GatewayConfig


def test_memory_config_has_curated_char_limits_with_defaults() -> None:
    cfg = GatewayConfig()
    assert cfg.memory.curated_memory_char_limit == 4000
    assert cfg.memory.curated_user_char_limit == 2000


def test_curated_char_limits_are_overridable() -> None:
    cfg = GatewayConfig(
        memory={"curated_memory_char_limit": 1000, "curated_user_char_limit": 500}
    )
    assert cfg.memory.curated_memory_char_limit == 1000
    assert cfg.memory.curated_user_char_limit == 500


def test_inject_limit_default_has_headroom_over_curated_budgets() -> None:
    """inject_limit must fit the full curated memory+user budgets plus header
    overhead at defaults, so the block-boundary truncation path in
    ``_load_curated_memory_block`` never drops the user block by default.
    """
    cfg = GatewayConfig()
    assert cfg.memory.inject_limit == 6400
    assert cfg.memory.inject_limit > (
        cfg.memory.curated_memory_char_limit + cfg.memory.curated_user_char_limit
    )
