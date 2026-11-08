"""TurnRunner stage decomposition — public exports.

Re-exports the eight TurnRunner stages, their I/O dataclasses, the
Protocol ports they accept, and the shared ``StageOutcome`` type.
Internal adapters and private dataclasses live in their owning
submodules (``harness``, ``stream_consumer_stage``,
``agent_bootstrap_stage``) and are imported from there by code that
needs them.
"""

from __future__ import annotations

from agentos.engine.turn_runner.agent_bootstrap_stage import (
    AgentBootstrapStage,
    AgentBootstrapStageInput,
    AgentBootstrapStageOutput,
    AgentConfigBuilderPort,
    AgentFactoryPort,
    MemorySnapshotPort,
    ModelCatalogPort,
    TimeoutBudgetPort,
)
from agentos.engine.turn_runner.attachment_stage import (
    AttachmentMessageBuilderPort,
    AttachmentStage,
    AttachmentStageInput,
    AttachmentStageOutput,
)
from agentos.engine.turn_runner.history_load_stage import (
    HistoryLoaderPort,
    HistoryLoadStage,
    HistoryLoadStageInput,
    HistoryLoadStageOutput,
    RequestContextPrependPort,
)
from agentos.engine.turn_runner.input_stage import (
    ExtraContextResolver,
    InputStage,
    InputStageInput,
    InputStageOutput,
    SessionAppendPort,
)
from agentos.engine.turn_runner.outcome import StageOutcome
from agentos.engine.turn_runner.prompt_assembler_stage import (
    MemoryFingerprintPort,
    PipelineExecutionPort,
    PromptAssemblerPort,
    PromptAssemblerStage,
    PromptAssemblerStageInput,
    PromptAssemblerStageOutput,
    PromptConfigResolverPort,
    PromptReportBuilderPort,
    RouterContextPort,
    RunPipelineRequest,
    SessionIdResolverPort,
)
from agentos.engine.turn_runner.provider_and_tools_stage import (
    ProviderAndToolsStage,
    ProviderAndToolsStageInput,
    ProviderAndToolsStageOutput,
    ProviderResolverPort,
    ToolBuilderPort,
)
from agentos.engine.turn_runner.stream_consumer_stage import (
    AgentRunPort,
    CompactionPersistPort,
    MemorySnapshotRefreshPort,
    MemorySyncNotifyPort,
    StreamConsumerStage,
    StreamConsumerStageInput,
    WarningTransformer,
)
from agentos.engine.turn_runner.turn_finalizer_stage import (
    CostRollupResult,
    SessionTotalsPort,
    TranscriptAppendPort,
    TurnErrorPersistPort,
    TurnFinalizerStage,
    TurnFinalizerStageInput,
    TurnFinalizerStageOutput,
    TurnMemoryCapturePort,
)

__all__ = [
    "AgentBootstrapStage",
    "AgentBootstrapStageInput",
    "AgentBootstrapStageOutput",
    "AgentConfigBuilderPort",
    "AgentFactoryPort",
    "AgentRunPort",
    "AttachmentMessageBuilderPort",
    "AttachmentStage",
    "AttachmentStageInput",
    "AttachmentStageOutput",
    "HistoryLoadStage",
    "HistoryLoadStageInput",
    "HistoryLoadStageOutput",
    "CompactionPersistPort",
    "CostRollupResult",
    "ExtraContextResolver",
    "HistoryLoaderPort",
    "InputStage",
    "InputStageInput",
    "InputStageOutput",
    "MemoryFingerprintPort",
    "MemorySnapshotPort",
    "MemorySnapshotRefreshPort",
    "MemorySyncNotifyPort",
    "ModelCatalogPort",
    "PipelineExecutionPort",
    "PromptAssemblerPort",
    "PromptAssemblerStage",
    "PromptAssemblerStageInput",
    "PromptAssemblerStageOutput",
    "PromptConfigResolverPort",
    "PromptReportBuilderPort",
    "ProviderAndToolsStage",
    "ProviderAndToolsStageInput",
    "ProviderAndToolsStageOutput",
    "ProviderResolverPort",
    "RequestContextPrependPort",
    "RouterContextPort",
    "RunPipelineRequest",
    "SessionAppendPort",
    "SessionIdResolverPort",
    "SessionTotalsPort",
    "StageOutcome",
    "StreamConsumerStage",
    "StreamConsumerStageInput",
    "TimeoutBudgetPort",
    "ToolBuilderPort",
    "TranscriptAppendPort",
    "TurnErrorPersistPort",
    "TurnFinalizerStage",
    "TurnFinalizerStageInput",
    "TurnFinalizerStageOutput",
    "TurnMemoryCapturePort",
    "WarningTransformer",
]
