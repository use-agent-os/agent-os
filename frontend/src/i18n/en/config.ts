import { defineNamespace } from '../registry'

export const config = defineNamespace('config', {
  documentTitle: 'Config - AgentOS Control',
  eyebrow: 'Control · Config',
  title: 'Config',
  subtitle:
    'Advanced gateway configuration. Use guided setup for provider, router, channels, and extras.',
  embeddedEyebrow: 'Advanced workspace',
  embeddedTitle: 'Configuration editor',
  embeddedSubtitle: 'Edit the complete runtime surface with a reviewed diff before it is applied.',

  // Header controls.
  modeLandmark: 'Editor mode',
  modeForm: 'Form',
  modeYaml: 'YAML',
  guidedTitle: 'Open guided setup',
  guided: 'Guided setup',
  reloadTitle: 'Reload config',
  reload: 'Reload',
  saveTitle: 'Save config',
  save: 'Save',

  // Load / conflict states.
  stateLoading: 'Loading configuration…',
  stateError: 'Configuration could not be loaded.',
  conflictBody:
    'Configuration changed while this draft was open. Discard the stale draft before saving against the latest revision.',
  conflictAction: 'Discard draft & reload',
  divergedBody:
    'The config file changed outside AgentOS. Writes are blocked until the gateway reloads or restarts with that file.',
  divergedAction: 'Refresh state',

  // Form surface.
  tabsLandmark: 'Config sections',
  tabCore: 'Core',
  tabAi: 'AI & Agents',
  tabMemory: 'Memory',
  tabCapabilities: 'Capabilities',
  tabConnections: 'Connections',
  tabSafety: 'Safety',
  tabRuntime: 'Runtime',
  tabOther: 'Other',
  tabSearchResults: 'Search results',
  searchLabel: 'Search config',
  searchPlaceholder: 'Search keys & values…',
  emptyNoMatches: 'No matching fields',
  groupGeneral: 'General',
  groupFields_one: '{count} field',
  groupFields_other: '{count} fields',
  fieldEnabled: 'Enabled',
  fieldDisabled: 'Disabled',
  fieldEdit: 'Edit',
  fieldInvalidJson: 'Invalid JSON',
  fieldHelpLabel: 'Help for {key}',
  fieldSecretShow: 'Show {key}',
  fieldSecretHide: 'Hide {key}',
  yamlEditorLabel: 'YAML editor',

  // Sticky save bar.
  pendingLandmark: 'Pending changes',
  // Not a tPlural pair: the sticky bar renders the number in its own <strong>
  // node (pinned by ConfigPage.test.tsx), and the catalog convention requires
  // {count} inside every _one/_other value. So the noun is selected directly.
  pendingChangeWord: 'change pending',
  pendingChangesWord: 'changes pending',
  diffHide: 'Hide diff',
  diffShow: 'View diff',
  discard: 'Discard',
  diffYamlKey: 'YAML',
  diffYamlOld: 'loaded config',
  diffYamlNew: 'unsaved draft',
  diffInvalidOld: 'loaded JSON',
  diffInvalidNew: 'Fix invalid JSON',

  // Toasts.
  toastLoadFailed: 'Failed to load config: {message}',
  toastSavedRestart: 'Config saved. Gateway restart required for the change to take effect.',
  toastSaved: 'Config saved',
  toastSaveFailed: 'Save failed: {message}',
  toastAppliedRestart: 'Config applied. Gateway restart required for the change to take effect.',
  toastApplied: 'Config applied',
  toastApplyFailed: 'Apply failed: {message}',
  toastWriteBlocked:
    'The config file changed outside AgentOS. Restart or reload the gateway first.',
  toastConflict: 'Configuration changed in another workspace. Discard and reload before saving.',
  toastPendingForm: 'Save or discard the pending Form changes before applying YAML.',
  toastPendingYaml: 'Save or discard the pending YAML change before saving Form fields.',
  toastFixJson: 'Fix invalid JSON before saving',
  toastNoChanges: 'No changes to save',
  toastReloadBlocked: 'Discard pending changes before reloading the configuration.',

  // Per-field help (config.js:32-125), keyed by config path.
  helpHost:
    'Network interface the gateway binds to. Read-only here — set via agentos gateway run --bind (CLI only). Defaults to 127.0.0.1 (loopback); 0.0.0.0 exposes on all interfaces and requires auth.',
  helpPort:
    'TCP port for the ASGI gateway. Read-only here — set via agentos gateway run --port (CLI only). Default 18791; the WebSocket and REST endpoints share it.',
  helpDebug:
    'Security-sensitive developer mode. Starlette debug, uvicorn log level, and some startup wiring need a gateway restart. Keep it off in shared deployments.',
  helpDiagnosticsEnabled:
    'Default standard diagnostics mode at gateway startup. Raw turn-call capture stays off unless AGENTOS_TURN_CALL_LOG=1 or the running gateway is switched with agentos diagnostics on --raw.',
  helpLogFileEnabled:
    'Writes gateway debug.log records for operator troubleshooting. This is separate from raw turn-call capture, which requires AGENTOS_TURN_CALL_LOG=1 or agentos diagnostics on --raw.',
  helpLogLevel: 'Minimum gateway file log level. AGENTOS_LOG_LEVEL can override this at runtime.',
  helpLogFileMaxBytes:
    'Maximum debug.log size before rotation. Set to 0 to disable rotation in the stdlib handler.',
  helpLogFileBackupCount: 'Number of rotated debug.log backups to retain.',
  helpAgentTokenSavingToolResultProjectionMaxInlineChars:
    'Maximum inline size for canonical tokenjuice tool-result projections. Raw tool output is transient and is not stored.',
  helpAgentosRouterEnabled:
    'Turn the auto tier router on or off. When off, every request uses the default model regardless of complexity.',
  helpAgentosRouterRolloutPhase:
    'Rollout stage for new router model versions. Higher phases enable more aggressive routing decisions.',
  helpAgentosRouterStrategy:
    '"pilot-v1" (default) classifies each turn with the local Pilot ML router (MiniLM+ONNX bundle, no LLM call); "llm_judge" classifies via a small LLM call instead. The pilot bundle ships in the wheel and degrades to the default tier if absent.',
  helpAgentosRouterJudgeModel:
    'Explicit LLM-judge model. Leave unset for Auto: the judge follows the tier profile’s cheapest text tier (c0 first), so profile switches auto-update it.',
  helpAgentosRouterJudgeProvider:
    'Optional provider for judge_model. Must match llm.provider — tier entries carry no credentials, so a cross-provider judge has no credential source.',
  helpAgentosRouterJudgeBaseUrl:
    'Local OpenAI-compatible judge endpoint (Ollama / LM Studio / llama.cpp / vLLM). Only takes effect when judge_model is set; the judge client is then built against this base URL with judge_api_key, bypassing the provider-match constraint (a local endpoint needs no cloud credentials).',
  helpAgentosRouterJudgeApiKey:
    'API key for the local judge endpoint (judge_base_url). Optional — local endpoints usually accept any token; a placeholder is used when unset. Redacted in logs.',
  helpAgentosRouterJudgeInputMaxChars:
    'Character budget for the message body sent to the judge (head/tail truncation with an elision marker). Signals are computed before truncation.',
  helpAgentosRouterJudgeShortCircuitEnabled:
    'Skip the judge call for trivial short greetings/acknowledgements (exact allowlist match) and route them to the cheapest tier directly.',
  helpAgentosRouterJudgeShortCircuitAllowlist:
    'Extra exact greeting/ack phrases (case-insensitive) that skip the judge. These are ADDED to the built-in default allowlist (en/vi/zh), not a replacement — leave empty to use just the defaults.',
  helpMemoryEmbedding:
    'Long-term memory embedding provider. Auto mode prefers a downloaded EmbeddingGemma model, then the bundled BGE ONNX, then a configured remote key, then FTS-only. Run `agentos memory embedding-download` to fetch the EmbeddingGemma upgrade; switching the local model triggers a full reindex. Remote embeddings require explicit memory embedding configuration.',
  helpMemoryEmbeddingProvider:
    'Canonical memory embedding provider: auto, none, local, openai/openai-compatible, or ollama. This is independent from the chat LLM provider.',
  helpMemoryEmbeddingRemoteApiKey:
    'API key for the memory embedding endpoint. This does not inherit the chat/OpenRouter key in auto mode.',
  helpMemoryEmbeddingRemoteBaseUrl:
    'OpenAI-compatible API root for memory indexing, for example https://api.openai.com/v1. The provider appends /embeddings.',
  helpMemoryEmbeddingLocalModel:
    'Optional local embedding model id to pin. Leave empty for auto (a downloaded EmbeddingGemma export when present, otherwise the bundled BGE-small). Set "google/embeddinggemma-300m" or "BAAI/bge-small-zh-v1.5" to force one. Changing this triggers a full reindex.',
  helpMemoryEmbeddingLocalOnnxDir:
    'Optional ONNX directory for a custom local embedding model. Leave empty to use the resolved model’s export (downloaded EmbeddingGemma or bundled BGE-small).',
  helpMemoryRetrievalMode:
    'Memory retrieval mode. "hybrid" uses vectors when an embedding provider is available; "fts_only" disables vectors.',
  helpMemoryCuratedMemoryCharLimit:
    'Character budget for MEMORY.md, the agent’s curated notes file. When full, the agent consolidates existing entries via the memory tool instead of growing the file further.',
  helpMemoryCuratedUserCharLimit: 'Character budget for USER.md, the curated user profile file.',
  helpMemoryInjectLimit:
    'Cap on the combined curated MEMORY.md + USER.md blocks injected into every system prompt. Keep it above the sum of the two char-limit budgets plus roughly 310 chars of header/separator overhead, or the user-profile block is dropped whole to stay under budget.',
  helpMemoryProviderName:
    'Optional external memory provider layered on top of built-in memory. Empty (the default) keeps built-in memory only; "mem0" enables the mem0 provider (prompt recall block, fenced recall, per-turn sync, write mirror). The provider is built once at boot, so changing this requires a gateway restart. mem0 needs the extra: pip install "use-agent-os[mem0]".',
  helpMemoryProviderMem0LlmProvider:
    'Backend the mem0 provider uses for its extraction/summarization LLM. Defaults to "ollama" for a fully local stack. Requires a gateway restart.',
  helpMemoryProviderMem0LlmModel:
    'mem0 extraction/summarization model. Default "qwen3:4b" (a small local Ollama model). Requires a gateway restart.',
  helpMemoryProviderMem0LlmBaseUrl:
    'Base URL for the mem0 LLM backend. Defaults to the local Ollama endpoint http://localhost:11434. Requires a gateway restart.',
  helpMemoryProviderMem0EmbedderProvider:
    'Backend for mem0 embeddings. Defaults to "ollama" so embeddings stay local. Requires a gateway restart.',
  helpMemoryProviderMem0EmbedderModel:
    'mem0 embedding model. Default "embeddinggemma" (local via Ollama). Requires a gateway restart.',
  helpMemoryProviderMem0EmbedderBaseUrl:
    'Base URL for the mem0 embedder backend. Defaults to the local Ollama endpoint http://localhost:11434. Requires a gateway restart.',
  helpMemoryProviderMem0VectorStorePath:
    'On-disk directory for the mem0 vector store. Empty resolves to <agent state dir>/mem0 at boot, keeping all data local. Requires a gateway restart.',
  helpSandboxSandbox:
    'Runtime sandbox switch. The out-of-box posture keeps this false; use agentos sandbox on|bypass|full to change sandbox and permission defaults together.',
  helpSandboxSecurityGrading:
    'Risk grading and approval gate for tool actions. Keep this paired with sandbox.sandbox unless using the sandbox CLI posture commands.',
  helpPermissionsDefaultMode:
    'Default interactive Control permission mode: bypass is the out-of-box local posture, off keeps sandboxed execution, on uses host execution with approvals, and full bypasses sensitive-path gates too.',
  helpPromptCacheMode:
    'Anthropic prompt cache control. "auto" (default) lets the provider decide; "on" forces caching; "off" disables it entirely.',
  helpContextBudgetTokens:
    'Soft cap on the assembled prompt size. When exceeded, the configured overflow policy kicks in (summarize, truncate, or refuse).',
  helpContextOverflowPolicy:
    '"auto_summarize" compacts older history via a small LLM; "hard_truncate" drops oldest turns; "refuse" rejects the turn with a stable error.',
  helpAuthMode:
    'Gateway auth scheme. "token" requires a static bearer token; "none" is open (loopback only); other modes per deployment.',
  helpControlUiAllowedOrigins:
    'Extra browser origins allowed to open the Control UI WebSocket, call the HTTP API, and send Host headers, beyond loopback (which is always allowed). Add your reverse-proxy origin here (e.g. https://agent.example.com) when serving the UI off another host; default ports 80/443 are normalized. Cross-origin requests are otherwise rejected to block cross-site WebSocket hijacking and DNS rebinding.',
  helpControlUiShowThinking:
    'Stream model reasoning ("thinking") into the WebUI live and expose it in chat history as collapsible blocks. WebUI-only: channel adapters (Slack, Telegram, …) never receive thinking regardless of this flag.',
  helpNone: 'No description yet — see the docs.',
} as const)
