"""The config settings that are the operator's alone -- one list for every gate.

``gateway.config_commit`` refuses an agent's config RPC writes to these roots,
holds an edit of the config file that touches them until the operator applies
it, and ``gateway.config_approval`` pins their operator-approved values so a
restart cannot apply an edit the operator never approved. ``env_policy``
refuses writes of their ``AGENTOS_GATEWAY_*`` environment spellings, since a
value in ``.env`` is the config at the next boot. Those used to be two copies
of the list, kept in step by a test; this module is the one source both
import, so it stays free of imports of its own.

A root belongs here when an agent setting it could raise its own limits or
run code as the gateway: the trading guardrails and endpoints, commands the
gateway itself runs (lifecycle hooks, MCP stdio servers, ACP harness launch
overrides, the skill install policy command), who may restart the gateway,
the tool policy, the default elevation, the sandbox, authentication, the
prompt-injection guards, the workspace confinement, how ACP harness
prompts are answered, which browser origins reach the Control surface, where
the state lives and where traces go.

:data:`AGENT_REFUSED_CONFIG_ROOTS` are refused to an agent's connection only:
where model, embedding, judge, image, audio and search calls go, the spend
ceilings and the agent definitions. Endpoints inside a keyed collection (a
provider profile, a channel) are not dotted roots;
:data:`OPERATOR_ONLY_ENTRY_FIELDS` names them per collection. Both are read by
``gateway.config_commit``.
"""

from __future__ import annotations

#: Dotted config paths (a top-level section, or one field of one) that only
#: the operator may change. See the module docstring for what each guards.
OPERATOR_ONLY_CONFIG_ROOTS: tuple[str, ...] = (
    # Limits, keys and the swap/bridge/RPC endpoints the signer trusts.
    "trading",
    # Lifecycle commands the gateway runs; the webhook ingress token.
    "hooks",
    # Tool allow/deny, the HTTP tools.invoke surface, SSRF exceptions.
    "tools",
    "permissions",
    "sandbox",
    "auth",
    "safety",
    "workspace_dir",
    "workspace_strict",
    "acp.permission_mode",
    "acp.non_interactive_permissions",
    # Per-harness launch overrides: a command, its args and its environment
    # the gateway executes, and which harnesses may be launched at all.
    "acp.agents",
    "acp.allowed_agents",
    "acp.allowed_cwds",
    "acp.install_missing_adapters",
    # chat_restart lets an agent's connection restart the gateway.
    "gateway_control",
    # Stdio servers are commands the gateway forks with its own environment.
    "mcp",
    # A command the gateway runs on every hub skill install.
    "skills.install_policy",
    # The browser binary the gateway executes, and attaching to the user's
    # own signed-in Chrome.
    "browser.binary_path",
    "browser.cdp_port",
    "browser.attach_confirmed",
    "browser.allow_unsafe_evaluate",
    # Who may open the Control surface from a browser, and on which listener.
    "cors",
    "control_ui.allowed_origins",
    "gateway.bind",
    "gateway.public_origin",
    # Where the wallets' ledger, sessions and the pid lock live.
    "state_dir",
    # Where traces (and their headers) are shipped.
    "observability.otlp_enabled",
    "observability.otlp_endpoint",
    "observability.otlp_headers",
)

#: Roots refused to a connection the gateway recognizes as an agent's (token,
#: process, window -- not ``unbound``: the user's web console keeps them) on
#: every config RPC writer, but *not* held from a file edit or pinned at boot
#: like :data:`OPERATOR_ONLY_CONFIG_ROOTS`: the operator's own CLI writes them
#: to the file while a gateway runs (``agentos providers configure``,
#: ``agentos agents add``), and the config file is already on an agent's
#: sensitive-path floor. An agent that points one of them elsewhere reads
#: every prompt and the key sent with it, lifts its own spend limit, or
#: defines an agent with any tools and workspace it likes.
AGENT_REFUSED_CONFIG_ROOTS: tuple[str, ...] = (
    # Where model calls -- every prompt, and the API key with it -- are sent.
    "llm.base_url",
    "llm.proxy",
    "agentos_router.judge_base_url",
    "agentos_router.jev.base_url",
    # Where embedding calls (memory contents) are sent.
    "memory.embedding.base_url",
    "memory.embedding.remote.base_url",
    "memory.embedding.ollama.base_url",
    "memory.provider.mem0.llm_base_url",
    "memory.provider.mem0.embedder_base_url",
    # Where image, audio and X search calls (and their keys) are sent.
    "image_generation.providers.openai.base_url",
    "image_generation.providers.openrouter.base_url",
    "image_generation.providers.surplus.base_url",
    "audio.providers.elevenlabs.base_url",
    "x_search.base_url",
    "search_proxy",
    "search_use_env_proxy",
    # Spend ceilings: an agent raising them lifts its own limit.
    "budgets",
    # Agent definitions: each one's tools, model, workspace and prompt
    # (``agents.create`` / ``agents.update`` write here).
    "agents",
)

#: Endpoint fields inside the entries of a keyed collection: a provider
#: profile (keyed by provider) and a channel (keyed by ``name``). Changing one
#: on an existing entry -- or giving a new entry one that differs from the
#: field's default -- sends that entry's credential, or its messages,
#: somewhere else.
OPERATOR_ONLY_ENTRY_FIELDS: dict[str, tuple[str, ...]] = {
    "provider_profiles": ("base_url", "proxy", "router.judge_base_url"),
    "channels.channels": (
        "api_base",
        "webhook_url",
        "gateway_url",
        "command_request_url",
        "imap_host",
        "imap_port",
        "smtp_host",
        "smtp_port",
    ),
}

#: Roots refused to *every* agent binding, ``unbound`` included -- not only to
#: a connection the gateway recognizes as an agent's. Nothing first-party
#: writes these through a config RPC (the web console, which is unbound on
#: loopback, has no control for them), so there is no operator flow to keep.
UNBOUND_REFUSED_CONFIG_ROOTS: tuple[str, ...] = (
    "trading",
    "gateway_control",
)

#: Settings models that read an environment prefix of their own besides the
#: nested ``AGENTOS_GATEWAY_<ROOT>__`` spelling. ``tests/test_env_policy.py``
#: checks that every operator-only root's model is covered here.
OPERATOR_ONLY_ENV_PREFIXES: tuple[str, ...] = (
    "AGENTOS_TRADING_",
    "AGENTOS_SANDBOX_",
    "AGENTOS_AUTH_",
    "AGENTOS_MCP_",
    "AGENTOS_CORS_",
    "AGENTOS_CONTROL_UI_ALLOWED_ORIGINS",
    "AGENTOS_BROWSER_BINARY_PATH",
    "AGENTOS_BROWSER_CDP_PORT",
    "AGENTOS_BROWSER_ATTACH_CONFIRMED",
    "AGENTOS_BROWSER_ALLOW_UNSAFE_EVALUATE",
    "AGENTOS_OBSERVABILITY_OTLP_",
    # ``SkillsConfig`` reads ``AGENTOS_SKILLS_<FIELD>``: the field itself (a
    # JSON value) and, nested, ``AGENTOS_SKILLS_INSTALL_POLICY__COMMAND``.
    "AGENTOS_SKILLS_INSTALL_POLICY",
)


def touches_root(path: str, roots: tuple[str, ...]) -> bool:
    """Whether *path* is one of *roots*, lies under one, or contains one."""
    text = str(path)
    return any(
        text == root or text.startswith(f"{root}.") or root.startswith(f"{text}.") for root in roots
    )


__all__ = [
    "AGENT_REFUSED_CONFIG_ROOTS",
    "OPERATOR_ONLY_CONFIG_ROOTS",
    "OPERATOR_ONLY_ENTRY_FIELDS",
    "OPERATOR_ONLY_ENV_PREFIXES",
    "UNBOUND_REFUSED_CONFIG_ROOTS",
    "touches_root",
]
