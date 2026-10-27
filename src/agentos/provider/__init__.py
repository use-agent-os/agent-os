"""agentos.provider — unified LLM provider abstraction layer."""

from .anthropic import AnthropicProvider
from .credentials import Credential, CredentialPool, NoCredentialsAvailable
from .failures import (
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .openai_responses import OpenAIResponsesProvider
from .protocol import (
    LLMProvider,
    ProviderFailure,
    ProviderMetadata,
    ProviderMetadataProvider,
    ProviderPlugin,
    provider_metadata,
    resolve_failover_chain,
    resolve_quota_status,
)
from .registry import (
    ProviderSpec,
    ProviderSupportLevel,
    UnknownProviderError,
    get_provider_spec,
    list_provider_names,
    list_provider_specs,
)
from .selector import (
    ModelSelector,
    ProviderBuildError,
    ProviderConfig,
    SelectorConfig,
    build_provider,
)
from .smart_routing import RefusalDecision, should_refuse
from .types import (
    ChatConfig,
    ContentBlockCompaction,
    ContentBlockDocument,
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    MessageProvenance,
    ModelCapabilities,
    ModelInfo,
    ProviderHeartbeatEvent,
    QuotaStatus,
    StopReason,
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolChoice,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    normalize_stop_reason,
)

__all__ = [
    # Protocol
    "LLMProvider",
    "ProviderPlugin",
    "ProviderFailure",
    "ProviderMetadata",
    "ProviderMetadataProvider",
    "provider_metadata",
    "ProviderFailureKind",
    "resolve_failover_chain",
    "resolve_quota_status",
    "ProviderRecoveryAction",
    "classify_provider_error",
    "decide_recovery_action",
    # Providers
    "AnthropicProvider",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
    "OllamaProvider",
    # Registry
    "ProviderSpec",
    "ProviderSupportLevel",
    "UnknownProviderError",
    "get_provider_spec",
    "list_provider_names",
    "list_provider_specs",
    # Selector
    "ModelSelector",
    "SelectorConfig",
    "ProviderConfig",
    "ProviderBuildError",
    "build_provider",
    # Credentials
    "Credential",
    "CredentialPool",
    "NoCredentialsAvailable",
    # Smart routing
    "RefusalDecision",
    "should_refuse",
    # Types
    "StopReason",
    "normalize_stop_reason",
    "StreamEvent",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "ToolUseStartEvent",
    "ToolUseDeltaEvent",
    "ToolUseEndEvent",
    "DoneEvent",
    "ErrorEvent",
    "ProviderHeartbeatEvent",
    "ModelCapabilities",
    "ModelInfo",
    "ChatConfig",
    "Message",
    "MessageProvenance",
    "QuotaStatus",
    "ToolChoice",
    "ToolDefinition",
    "ToolInputSchema",
    "ContentBlockText",
    "ContentBlockThinking",
    "ContentBlockRedactedThinking",
    "ContentBlockToolUse",
    "ContentBlockToolResult",
    "ContentBlockCompaction",
    "ContentBlockDocument",
]
