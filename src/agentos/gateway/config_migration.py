"""Pre-validation migration for user-owned gateway config files."""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import tempfile
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from agentos.paths import default_agentos_home
from agentos.router_strategies import PILOT_STRATEGY_ID, V4_STRATEGY_ID
from agentos.skills.injector import DEFAULT_MAX_SKILLS_PROMPT_CHARS

DEPRECATED_MEMORY_FIELDS: frozenset[str] = frozenset(
    {
        "memory.profile",
        "memory.cost.embedding_cache",
        "memory.cost.rerank_cache",
        "memory.cost.llm_judge_cache",
        "memory.facts_enabled",
        "memory.facts_top_k",
        "memory.facts_max_chars",
        "memory.multi_hop_enabled",
        "memory.multi_hop_max_depth",
        "memory.multi_hop_score_threshold",
        "memory.recall_frequency",
        "memory.recall_top_k_default",
        "memory.auto_recall_enabled",
        "memory.prefetch_enabled",
        "memory.prefetch_max_results",
        "memory.prefetch_min_score",
        "memory.prefetch_total_max_chars",
        "memory.semantic_chunking_enabled",
        "memory.eviction_policy",
        "memory.summary_model",
        "memory.summary_max_tokens",
        # Dream consolidation was removed; MemoryConfig forbids extras, so an
        # existing agentos.toml carrying [memory.dream] would fail validation
        # at boot without this. The whole sub-table is dropped and warned about.
        "memory.dream",
        # Session flush was removed; MemoryConfig forbids extras, so an
        # existing agentos.toml carrying these would fail validation at boot.
        #
        # The safety-gate keys go too, and dropping them is load-bearing rather
        # than cosmetic. No flush service is constructed any more, so no receipt
        # can ever be produced. Left in place, `flush_enabled = true` with
        # `block` (or the legacy `requires_safe_receipt`) makes compaction demand
        # a receipt that nothing can write: it refuses on every turn, the context
        # window fills, and the only clue is one warning line. Removing the key
        # forces `pre_compaction_flush_enabled()` false for good.
        "memory.flush_enabled",
        "memory.flush_compaction_requires_safe_receipt",
        "memory.flush_compaction_safety_mode",
        "memory.flush_timeout_seconds",
        "memory.flush_background_timeout_seconds",
        "memory.flush_backoff_initial_seconds",
        "memory.flush_backoff_max_seconds",
        "memory.flush_archive_max_bytes",
        "memory.repair_enabled",
        "memory.repair_interval_seconds",
        "memory.repair_max_items_per_tick",
        # Daily-note char budgets were removed in PR #111; the config keys
        # were left behind as dead weight. MemoryConfig forbids extras, so an
        # existing agentos.toml carrying these would fail validation at boot
        # without this migration entry.
        "memory.daily_note_max_chars",
        "memory.daily_notes_total_max_chars",
    }
)

DEPRECATED_COST_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("memory.cost.") for k in DEPRECATED_MEMORY_FIELDS if k.startswith("memory.cost.")
)
DEPRECATED_MEMORY_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("memory.")
    for k in DEPRECATED_MEMORY_FIELDS
    if k.startswith("memory.") and not k.startswith("memory.cost.")
)

# The Bankr gateway serves bare model ids; the older "virtuals/" namespace is
# mapped to its bare form for configs that still carry it. Removed ids
# (kimi-k2.6) and retired defaults (minimax-m3, the image tier until Claude
# Haiku 5.5 took it) map to the new default for their old tier role. Superseded
# defaults that still resolve upstream (claude-opus-4.8) are refreshed to the
# current default for the same tier, mirroring LEGACY_OPENROUTER_MODEL_IDS.
LEGACY_GATEWAY_MODEL_IDS: dict[str, str] = {
    "virtuals/minimax-m3": "claude-haiku-5.5",
    "virtuals/deepseek-v4-flash": "deepseek-v4-flash",
    "virtuals/qwen3.7-max": "qwen3.7-max",
    "virtuals/claude-opus-4.8": "claude-opus-5.5",
    "virtuals/kimi-k2.6": "claude-haiku-5.5",
    "claude-opus-4.8": "claude-opus-5.5",
}

# Prior OpenRouter tier defaults, mapped to the defaults that replaced them.
# Unlike the "virtuals/" ids these still resolve upstream, so the rewrite only
# refreshes configs that carried an old *default* forward — the same treatment
# "virtuals/kimi-k2.6" got when its gateway default was replaced.
LEGACY_OPENROUTER_MODEL_IDS: dict[str, str] = {
    "deepseek/deepseek-v4-pro": "anthropic/claude-haiku-5.5",
    "z-ai/glm-5.1": "anthropic/claude-sonnet-5.5",
    "anthropic/claude-opus-4.7": "anthropic/claude-opus-5.5",
    "anthropic/claude-opus-4.8": "anthropic/claude-opus-5.5",
    "moonshotai/kimi-k2.6": "anthropic/claude-haiku-5.5",
}

#: The skills-block budget every config written before 2026-07 carries. Only
#: this exact value is refreshed to the current default — see the migration at
#: the end of :func:`migrate_config_payload`.
LEGACY_MAX_SKILLS_PROMPT_CHARS = 8000

#: Every default the budget has ever been materialised as, the current one
#: included. A config carrying one of these was written by us, not chosen by an
#: operator, so it follows the default when the default moves; any other value
#: is left alone. Add the old value here whenever the default is raised.
STALE_MAX_SKILLS_PROMPT_CHARS: frozenset[int] = frozenset(
    {LEGACY_MAX_SKILLS_PROMPT_CHARS, 24000, 26000, 28000}
)

OPENROUTER_PROVIDER_ID = "openrouter"

#: ``agentos.trading.providers.DEFAULT_PROVIDER_ID``, repeated rather than
#: imported: config migration runs at boot, before the trading package (and
#: its HTTP stack) has any reason to be loaded. ``test_trading_migration``
#: asserts the two stay equal.
DEFAULT_SWAP_PROVIDER = "aggregator"

GATEWAY_PROVIDER_ID = "bankr"
# Historical aliases ("capgateway" / "opencap-gateway") are intentionally not
# rewritten. Canonical ``opencap`` is a supported provider again, so existing
# configurations using that id remain valid without migration.
LEGACY_GATEWAY_PROVIDER_IDS: tuple[str, ...] = ()
LEGACY_ROUTER_SECTION = "cap_router"

# Channel adapters retired from the built-in runtime. Persisted entries are
# removed before strict channel validation so an upgrade can still boot and
# atomically rewrite the user's config. QQ aliases are migration-only: they do
# not become accepted public channel type ids.
RETIRED_CHANNEL_TYPE_ALIASES: dict[str, str] = {
    "dingtalk": "dingtalk",
    "matrix": "matrix",
    "qq": "qq",
    "qq-bot": "qq",
    "qq_bot": "qq",
    "qqbot": "qq",
    "wecom": "wecom",
}

DEPRECATED_SANDBOX_FIELDS: frozenset[str] = frozenset(
    {
        # sandbox.network_default was a silent no-op: no code read it, and its
        # only non-default value (proxy_allowlist) is unimplemented on both
        # sandbox backends. Dropped so existing agentos.toml files carrying it
        # keep loading instead of silently doing nothing.
        "sandbox.network_default",
    }
)
DEPRECATED_SANDBOX_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("sandbox.") for k in DEPRECATED_SANDBOX_FIELDS
)

DEPRECATED_AGENT_TOKEN_SAVING_FIELDS: frozenset[str] = frozenset(
    {
        "agent_token_saving.tool_result_compression_enabled",
        "agent_token_saving.tool_result_compression_mode",
        "agent_token_saving.tool_result_compression_max_share",
        "agent_token_saving.tool_result_compression_summary_model",
        "agent_token_saving.tool_result_compression_summary_max_tokens",
        "agent_token_saving.tool_result_compression_summary_timeout_seconds",
        "agent_token_saving.tool_result_compression_summary_input_max_chars",
    }
)
DEPRECATED_AGENT_TOKEN_SAVING_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("agent_token_saving.") for k in DEPRECATED_AGENT_TOKEN_SAVING_FIELDS
)

# Auto-archive was removed. SubagentsGatewayConfig is a plain BaseModel and
# Pydantic's default extra="ignore" means a toml carrying the key would still
# load — but the dead key would silently linger in the user's file. This
# migration strips it, rewrites the toml with a backup, and warns the user
# that the setting is now a no-op.
DEPRECATED_SUBAGENTS_FIELDS: frozenset[str] = frozenset(
    {
        "subagents.archive_after_minutes",
    }
)
DEPRECATED_SUBAGENTS_LEAVES: frozenset[str] = frozenset(
    k.removeprefix("subagents.") for k in DEPRECATED_SUBAGENTS_FIELDS
)

_LEGACY_MEMORY_FIELDS_WARN_LOCK = threading.Lock()
_LEGACY_MEMORY_FIELDS_WARNED = False
_LEGACY_MEMORY_FIELDS_SEEN: set[str] = set()
_LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARN_LOCK = threading.Lock()
_LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED = False
_LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN: set[str] = set()
_LEGACY_SUBAGENTS_FIELDS_WARN_LOCK = threading.Lock()
_LEGACY_SUBAGENTS_FIELDS_WARNED = False
_LEGACY_SUBAGENTS_FIELDS_SEEN: set[str] = set()


@dataclass(frozen=True)
class ConfigMigrationResult:
    payload: dict[str, Any]
    changes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    removed_fields: tuple[str, ...] = ()
    changed: bool = False


@dataclass
class _MigrationBuilder:
    payload: dict[str, Any]
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    removed_fields: list[str] = field(default_factory=list)

    def result(self) -> ConfigMigrationResult:
        changed = bool(self.changes or self.removed_fields)
        return ConfigMigrationResult(
            payload=self.payload,
            changes=tuple(self.changes),
            warnings=tuple(self.warnings),
            removed_fields=tuple(self.removed_fields),
            changed=changed,
        )


def handle_deprecated_memory_fields(
    found: dict[str, object],
    source: str,
) -> None:
    """Record and warn once for deprecated memory fields removed from config data."""
    global _LEGACY_MEMORY_FIELDS_WARNED

    if not found:
        return

    with _LEGACY_MEMORY_FIELDS_WARN_LOCK:
        _LEGACY_MEMORY_FIELDS_SEEN.update(found.keys())
        should_warn = not _LEGACY_MEMORY_FIELDS_WARNED
        if should_warn:
            _LEGACY_MEMORY_FIELDS_WARNED = True
            warning_fields = sorted(_LEGACY_MEMORY_FIELDS_SEEN)
        else:
            warning_fields = []

    _write_legacy_field_log(found, source)

    if should_warn:
        n = len(warning_fields)
        first_three = ", ".join(warning_fields[:3])
        try:
            logs_dir = default_agentos_home() / "logs"
            log_ref = str(logs_dir)
        except Exception:
            log_ref = "~/.agentos/logs"
        warnings.warn(
            f"AgentOS: {n} legacy memory.* config field(s) ignored "
            f"(e.g. {first_three}); see {log_ref} for details. "
            f"These fields will be removed in 0.2.0.",
            DeprecationWarning,
            stacklevel=6,
        )
        logging.getLogger(__name__).warning(
            "AgentOS: %d legacy memory.* config field(s) ignored (e.g. %s); "
            "see %s for details. These fields will be removed in 0.2.0.",
            n,
            first_three,
            log_ref,
        )


def handle_deprecated_agent_token_saving_fields(
    found: dict[str, object],
    source: str,
) -> None:
    """Record and warn once for deprecated token-saving fields removed from config data."""
    global _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED

    if not found:
        return

    with _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARN_LOCK:
        _LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN.update(found.keys())
        should_warn = not _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED
        if should_warn:
            _LEGACY_AGENT_TOKEN_SAVING_FIELDS_WARNED = True
            warning_fields = sorted(_LEGACY_AGENT_TOKEN_SAVING_FIELDS_SEEN)
        else:
            warning_fields = []

    _write_legacy_field_log(found, source)

    if should_warn:
        n = len(warning_fields)
        first_three = ", ".join(warning_fields[:3])
        try:
            logs_dir = default_agentos_home() / "logs"
            log_ref = str(logs_dir)
        except Exception:
            log_ref = "~/.agentos/logs"
        warnings.warn(
            f"AgentOS: {n} legacy agent_token_saving.tool_result_compression_* "
            f"config field(s) migrated or ignored (e.g. {first_three}); see "
            f"{log_ref} for details. Tokenjuice projection is now the built-in "
            "tool-result path.",
            DeprecationWarning,
            stacklevel=6,
        )
        logging.getLogger(__name__).warning(
            "AgentOS: %d legacy agent_token_saving.tool_result_compression_* "
            "config field(s) migrated or ignored (e.g. %s); see %s for details. "
            "Tokenjuice projection is now the built-in tool-result path.",
            n,
            first_three,
            log_ref,
        )


def handle_deprecated_subagents_fields(
    found: dict[str, object],
    source: str,
) -> None:
    """Record and warn once for deprecated subagents fields removed from config data."""
    global _LEGACY_SUBAGENTS_FIELDS_WARNED

    if not found:
        return

    with _LEGACY_SUBAGENTS_FIELDS_WARN_LOCK:
        _LEGACY_SUBAGENTS_FIELDS_SEEN.update(found.keys())
        should_warn = not _LEGACY_SUBAGENTS_FIELDS_WARNED
        if should_warn:
            _LEGACY_SUBAGENTS_FIELDS_WARNED = True
            warning_fields = sorted(_LEGACY_SUBAGENTS_FIELDS_SEEN)
        else:
            warning_fields = []

    _write_legacy_field_log(found, source)

    if should_warn:
        n = len(warning_fields)
        first_three = ", ".join(warning_fields[:3])
        try:
            logs_dir = default_agentos_home() / "logs"
            log_ref = str(logs_dir)
        except Exception:
            log_ref = "~/.agentos/logs"
        warnings.warn(
            f"AgentOS: {n} legacy subagents.* config field(s) ignored "
            f"(e.g. {first_three}); see {log_ref} for details. "
            "These fields will be removed in 0.2.0.",
            DeprecationWarning,
            stacklevel=6,
        )
        logging.getLogger(__name__).warning(
            "AgentOS: %d legacy subagents.* config field(s) ignored (e.g. %s); "
            "see %s for details. These fields will be removed in 0.2.0.",
            n,
            first_three,
            log_ref,
        )


def _write_legacy_field_log(found: dict[str, object], source: str) -> None:
    try:
        logs_dir = default_agentos_home() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        iso_now = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        log_path = logs_dir / f"legacy_config_{iso_now}.log"
        with log_path.open("a", encoding="utf-8") as fh:
            for leaf, value in found.items():
                entry = {
                    "timestamp": datetime.datetime.now(tz=datetime.UTC).isoformat(),
                    "field": leaf,
                    "source": source,
                    "value_repr": str(value)[:200],
                }
                fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def migrate_config_payload(data: dict[str, Any]) -> ConfigMigrationResult:
    """Return a config payload upgraded for the current strict schema.

    Call this only at user-owned disk-load boundaries, before GatewayConfig
    validates the payload.
    """
    builder = _MigrationBuilder(payload=copy.deepcopy(data))

    def _is_gateway_provider(provider: object) -> bool:
        # Both llm.provider and tier provider default to the gateway when unset.
        if provider is None:
            return True
        provider_id = str(provider).strip().lower()
        return provider_id == GATEWAY_PROVIDER_ID or provider_id in LEGACY_GATEWAY_PROVIDER_IDS

    def _rename_gateway_provider(container: object, key: str, path: str) -> None:
        if not isinstance(container, dict):
            return
        current = str(container.get(key) or "").strip().lower()
        if current in LEGACY_GATEWAY_PROVIDER_IDS:
            container[key] = GATEWAY_PROVIDER_ID
            builder.changes.append(f"{path}: {current} -> {GATEWAY_PROVIDER_ID}")

    # 2026-07 rename: the [cap_router] section became [agentos_router]. Upgrade old
    # on-disk configs so the strict schema does not reject them. The gateway
    # provider rename is deliberately not migrated (see LEGACY_GATEWAY_PROVIDER_IDS).
    if LEGACY_ROUTER_SECTION in builder.payload:
        legacy_router = builder.payload.pop(LEGACY_ROUTER_SECTION)
        if "agentos_router" in builder.payload:
            builder.removed_fields.append(LEGACY_ROUTER_SECTION)
        else:
            builder.payload["agentos_router"] = legacy_router
            builder.changes.append(f"{LEGACY_ROUTER_SECTION} -> agentos_router")

    auth_section = builder.payload.get("auth")
    if isinstance(auth_section, dict):
        legacy_scopes = auth_section.pop("token_scopes", None)
        if legacy_scopes is not None:
            normalized = {
                str(item).strip()
                for item in (legacy_scopes if isinstance(legacy_scopes, list) else [])
                if str(item).strip()
            }
            if normalized and "operator.admin" not in normalized:
                raise ValueError(
                    "Legacy auth.token_scopes configured a limited token. "
                    "AgentOS now uses binary Control access and will not silently "
                    "widen that token. Remove token_scopes only after confirming "
                    "the token may control the full gateway."
                )
            builder.removed_fields.append("auth.token_scopes")
        for field_name in ("allowed_roles",):
            if field_name in auth_section:
                auth_section.pop(field_name)
                builder.removed_fields.append(f"auth.{field_name}")
        if auth_section.pop("allow_unauthenticated_public", False):
            raise ValueError(
                "auth.allow_unauthenticated_public is no longer supported. "
                "Configure token authentication before using a non-loopback bind."
            )
        if "allow_unauthenticated_public" in data.get("auth", {}):
            builder.removed_fields.append("auth.allow_unauthenticated_public")

    if "channel_admin_senders" in builder.payload:
        builder.payload.pop("channel_admin_senders")
        builder.removed_fields.append("channel_admin_senders")

    channels_section = builder.payload.get("channels")
    if isinstance(channels_section, dict) and isinstance(
        configured_channels := channels_section.get("channels"), list
    ):
        retained_channels: list[Any] = []
        retired_type_counts: dict[str, int] = {}
        for entry in configured_channels:
            raw_type = entry.get("type") if isinstance(entry, dict) else None
            normalized_type = raw_type.strip().lower() if isinstance(raw_type, str) else ""
            retired_type = RETIRED_CHANNEL_TYPE_ALIASES.get(normalized_type)
            if retired_type is None:
                if isinstance(entry, dict) and normalized_type == "telegram":
                    had_access_mode = "access_mode" in entry
                    access_mode = str(entry.pop("access_mode", "pairing") or "pairing")
                    if access_mode == "disabled":
                        entry["enabled"] = False
                    for field_name in (
                        "approved_sender_ids",
                        "group_access_mode",
                        "group_allowed_sender_ids",
                    ):
                        if field_name in entry:
                            entry.pop(field_name)
                            builder.removed_fields.append(f"channels.channels[].{field_name}")
                    if had_access_mode:
                        # Per-entry reporting below is intentionally generic so
                        # channel names and sender ids never enter migration logs.
                        builder.removed_fields.append("channels.channels[].access_mode")
                    entry.setdefault("groups_enabled", False)
                retained_channels.append(entry)
                continue
            retired_type_counts[retired_type] = retired_type_counts.get(retired_type, 0) + 1

        if retired_type_counts:
            channels_section["channels"] = retained_channels
            removed_count = sum(retired_type_counts.values())
            entry_label = "entry" if removed_count == 1 else "entries"
            type_summary = ", ".join(
                f"{channel_type}={retired_type_counts[channel_type]}"
                for channel_type in sorted(retired_type_counts)
            )
            builder.changes.append(
                "channels.channels: removed "
                f"{removed_count} retired channel adapter {entry_label} ({type_summary})"
            )

    _rename_gateway_provider(builder.payload.get("llm"), "provider", "llm.provider")
    router_section = builder.payload.get("agentos_router")
    if isinstance(router_section, dict):
        _rename_gateway_provider(router_section, "tier_profile", "agentos_router.tier_profile")

    llm = builder.payload.get("llm")
    if isinstance(llm, dict) and _is_gateway_provider(llm.get("provider")):
        old_model = str(llm.get("model") or "").strip()
        new_model = LEGACY_GATEWAY_MODEL_IDS.get(old_model)
        if new_model:
            llm["model"] = new_model
            builder.changes.append(f"llm.model: {old_model} -> {new_model}")

    def _is_openrouter_provider(provider: object) -> bool:
        return str(provider or "").strip().lower() == OPENROUTER_PROVIDER_ID

    if isinstance(llm, dict) and _is_openrouter_provider(llm.get("provider")):
        old_model = str(llm.get("model") or "").strip()
        new_model = LEGACY_OPENROUTER_MODEL_IDS.get(old_model)
        if new_model:
            llm["model"] = new_model
            builder.changes.append(f"llm.model: {old_model} -> {new_model}")

    agentos_router = builder.payload.get("agentos_router")
    if isinstance(agentos_router, dict):
        # Force default-flip: pilot-v1 is the default strategy, but historical
        # onboarding once persisted `strategy = "v4_phase3"` explicitly, so an upgraded
        # install would silently stay on the legacy router. Unconditionally rewrite
        # a persisted v4_phase3 to pilot-v1 on load — there is no supported way to
        # keep v4_phase3 in config; the legacy engine and its model bundle were
        # removed from the tree (Phase C). The rewrite is idempotent:
        # once flipped the value is pilot-v1, so re-running is a no-op. "pilot-v1"
        # and "llm_judge" configs are left untouched, and a config with no strategy
        # key already resolves to the default and is not rewritten here.
        strategy = agentos_router.get("strategy")
        if isinstance(strategy, str) and strategy.strip() == V4_STRATEGY_ID:
            agentos_router["strategy"] = PILOT_STRATEGY_ID
            builder.changes.append(
                f"agentos_router.strategy: {V4_STRATEGY_ID} -> {PILOT_STRATEGY_ID} "
                "(default flip; legacy router auto-migrated on load)"
            )
            logging.getLogger(__name__).info(
                "Pilot router strategy force-migrated %s -> %s on config load",
                V4_STRATEGY_ID,
                PILOT_STRATEGY_ID,
            )

        # confidence_threshold gained a strict [0.0, 1.0] bound with the judge
        # migration. Under the old v4 confidence gate an out-of-range value was a
        # legitimate, functioning knob (e.g. >1.0 forced always-gate-to-default,
        # since a probability can never exceed a >1.0 threshold). A stale TOML
        # carrying such a value would now fail schema validation and crash the
        # gateway on boot. Clamp it into range so old configs boot cleanly, the
        # same way the strategy flip is normalized above.
        raw_threshold = agentos_router.get("confidence_threshold")
        if isinstance(raw_threshold, (int, float)) and not isinstance(raw_threshold, bool):
            clamped = min(1.0, max(0.0, float(raw_threshold)))
            if clamped != raw_threshold:
                agentos_router["confidence_threshold"] = clamped
                builder.changes.append(
                    "agentos_router.confidence_threshold: "
                    f"{raw_threshold} -> {clamped} (clamped to [0.0, 1.0])"
                )
    tiers = agentos_router.get("tiers") if isinstance(agentos_router, dict) else None
    if isinstance(tiers, dict):
        for tier_name, tier in tiers.items():
            if not isinstance(tier, dict):
                continue
            if _is_openrouter_provider(tier.get("provider")):
                old_model = str(tier.get("model") or "").strip()
                new_model = LEGACY_OPENROUTER_MODEL_IDS.get(old_model)
                if new_model:
                    tier["model"] = new_model
                    builder.changes.append(
                        f"agentos_router.tiers.{tier_name}.model: {old_model} -> {new_model}"
                    )
                continue
            if not _is_gateway_provider(tier.get("provider")):
                continue
            _rename_gateway_provider(tier, "provider", f"agentos_router.tiers.{tier_name}.provider")
            old_model = str(tier.get("model") or "").strip()
            new_model = LEGACY_GATEWAY_MODEL_IDS.get(old_model)
            if new_model:
                tier["model"] = new_model
                builder.changes.append(
                    f"agentos_router.tiers.{tier_name}.model: {old_model} -> {new_model}"
                )

    memory = builder.payload.get("memory")
    if isinstance(memory, dict):
        if memory.get("capture_mode") == "archive_turn_pair":
            memory["capture_mode"] = "turn_pair"
            builder.changes.append("memory.capture_mode: archive_turn_pair -> turn_pair")

        if "index_captured_turns" in memory:
            value = memory.pop("index_captured_turns")
            builder.removed_fields.append("memory.index_captured_turns")
            if bool(value):
                builder.warnings.append(
                    "memory.index_captured_turns was removed; captured turns are no "
                    "longer indexed into normal recall"
                )

        deprecated: dict[str, object] = {}
        for leaf in list(memory):
            if leaf in DEPRECATED_MEMORY_LEAVES:
                deprecated[f"memory.{leaf}"] = memory.pop(leaf)

        cost = memory.get("cost")
        if isinstance(cost, dict):
            for leaf in list(cost):
                if leaf in DEPRECATED_COST_LEAVES:
                    deprecated[f"memory.cost.{leaf}"] = cost.pop(leaf)
            if not cost:
                memory.pop("cost", None)

        if deprecated:
            builder.removed_fields.extend(sorted(deprecated))
            handle_deprecated_memory_fields(deprecated, "config_migration")

    sandbox_section = builder.payload.get("sandbox")
    if isinstance(sandbox_section, dict):
        deprecated_sandbox: dict[str, object] = {}
        for leaf in list(sandbox_section):
            if leaf in DEPRECATED_SANDBOX_LEAVES:
                deprecated_sandbox[f"sandbox.{leaf}"] = sandbox_section.pop(leaf)
        if deprecated_sandbox:
            builder.removed_fields.extend(sorted(deprecated_sandbox))
            builder.warnings.append(
                "sandbox.network_default was removed; no code read it and "
                "proxy_allowlist is unimplemented on both sandbox backends"
            )

    token_saving = builder.payload.get("agent_token_saving")
    if isinstance(token_saving, dict):
        summary_input_leaf = "tool_result_compression_summary_input_max_chars"
        projection_leaf = "tool_result_projection_max_inline_chars"
        if summary_input_leaf in token_saving and projection_leaf not in token_saving:
            token_saving[projection_leaf] = token_saving[summary_input_leaf]
            builder.changes.append(
                "agent_token_saving.tool_result_compression_summary_input_max_chars "
                "-> agent_token_saving.tool_result_projection_max_inline_chars"
            )

        deprecated_token_saving: dict[str, object] = {}
        for leaf in list(token_saving):
            if leaf in DEPRECATED_AGENT_TOKEN_SAVING_LEAVES:
                deprecated_token_saving[f"agent_token_saving.{leaf}"] = token_saving.pop(leaf)

        if deprecated_token_saving:
            builder.removed_fields.extend(sorted(deprecated_token_saving))
            handle_deprecated_agent_token_saving_fields(
                deprecated_token_saving,
                "config_migration",
            )
            if (
                deprecated_token_saving.get("agent_token_saving.tool_result_compression_enabled")
                is False
                or deprecated_token_saving.get("agent_token_saving.tool_result_compression_mode")
                == "off"
            ):
                builder.warnings.append(
                    "agent_token_saving.tool_result_compression_* was removed; "
                    "tokenjuice projection is now the built-in tool-result path"
                )

    subagents_section = builder.payload.get("subagents")
    if isinstance(subagents_section, dict):
        deprecated_subagents: dict[str, object] = {}
        for leaf in list(subagents_section):
            if leaf in DEPRECATED_SUBAGENTS_LEAVES:
                deprecated_subagents[f"subagents.{leaf}"] = subagents_section.pop(leaf)

        if deprecated_subagents:
            builder.removed_fields.extend(sorted(deprecated_subagents))
            handle_deprecated_subagents_fields(deprecated_subagents, "config_migration")

    # 2026-09: the KyberSwap provider was removed and the AgentOS Aggregator
    # became the default. TradingConfig forbids extras, so an existing
    # agentos.toml carrying `kyber_client_id` would fail validation at boot;
    # and `provider = "kyber"` now names a provider that no longer exists.
    # Both are rewritten rather than rejected, so an upgrade keeps trading.
    trading_section = builder.payload.get("trading")
    if isinstance(trading_section, dict):
        if "kyber_client_id" in trading_section:
            trading_section.pop("kyber_client_id")
            builder.removed_fields.append("trading.kyber_client_id")
        if str(trading_section.get("provider") or "").strip().lower() == "kyber":
            trading_section["provider"] = DEFAULT_SWAP_PROVIDER
            builder.changes.append(f"trading.provider: kyber -> {DEFAULT_SWAP_PROVIDER}")
            builder.warnings.append(
                "The KyberSwap provider was removed; swaps now route through the "
                "AgentOS Aggregator, which needs no API key"
            )

    # 2026-07: the skills-block budget default rose from 8000 to 24000 once the
    # block stopped emitting a filesystem path per skill. 8000 could not fit the
    # descriptions for the shipped set, so every install that saved a config was
    # pinned to a name-only skill list — the raised default alone would never
    # have reached them. The same happened again at 24000 and 26000 as bundled
    # skills were added. Refresh only a value that was itself a default, the
    # same rule the legacy model ids use: a value someone chose deliberately is
    # left alone.
    skills_section = builder.payload.get("skills")
    if isinstance(skills_section, dict):
        current_budget = skills_section.get("max_skills_prompt_chars")
        if (
            isinstance(current_budget, int)
            and not isinstance(current_budget, bool)
            and current_budget in STALE_MAX_SKILLS_PROMPT_CHARS
            and current_budget != DEFAULT_MAX_SKILLS_PROMPT_CHARS
        ):
            skills_section["max_skills_prompt_chars"] = DEFAULT_MAX_SKILLS_PROMPT_CHARS
            builder.changes.append(
                f"skills.max_skills_prompt_chars: {current_budget} -> "
                f"{DEFAULT_MAX_SKILLS_PROMPT_CHARS}"
            )

    return builder.result()


def backup_and_write_migrated_config(
    path: str | Path,
    payload: dict[str, Any],
    result: ConfigMigrationResult,
) -> Path:
    """Back up and atomically replace a migrated user config file."""
    target = Path(path)
    backup = make_config_backup(target)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            tomli_w.dump(payload, fh)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    os.chmod(target, 0o600)
    logging.getLogger(__name__).warning(
        "AgentOS config migrated for 0.2.0 schema",
        extra={
            "path": str(target),
            "backup": str(backup),
            "changes": list(result.changes),
            "removed_fields": list(result.removed_fields),
            "warnings": list(result.warnings),
        },
    )
    return backup


def make_config_backup(target: str | Path) -> Path:
    """Create a collision-safe 0600 backup next to a config file."""
    source = Path(target)
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    data = source.read_bytes()

    for attempt in range(1000):
        suffix = "" if attempt == 0 else f".{attempt}"
        backup = source.with_name(f"{source.name}.backup.{stamp}{suffix}")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except Exception:
            try:
                os.unlink(backup)
            except OSError:
                pass
            raise
        backup.chmod(0o600)
        return backup

    raise FileExistsError(f"Could not create unique backup for {source}")
