import { defineNamespace } from '../registry'

export const chat = defineNamespace('chat', {
  // Page chrome.
  srTitle: 'Chat',
  newChat: 'New chat',
  newChatTitle: 'New chat ({shortcut})',
  sessionControls: 'Chat session controls',
  opening: 'Opening conversation…',
  jumpToLatest: 'Jump to latest',
  toolOutputEyebrow: 'Tool output',
  toolOutputClose: 'Close tool output',
  toolOutputFull: 'Full result',
  toolOutputChars: '{count} characters',

  // Composer.
  runModes: 'Run modes',
  runModesTitle: 'Run modes: execution and routing',
  runModesClose: 'Close run modes',
  attachFiles: 'Attach files',
  attachFileTitle: 'Attach a file',
  messagePlaceholder: 'Send a message...',
  messageLabel: 'Message',
  stopTitle: 'Stop (Esc)',
  stop: 'Stop',
  send: 'Send',

  // Attachments.
  attachmentIconFallback: 'file',
  attachmentRemove: 'Remove',
  attachmentRemoveAria: 'Remove attachment {name}',

  // Pending queue.
  pendingLandmark: 'Pending messages',
  pendingLabelTitle:
    'Alt+↑ pulls the most recent back into the input · ESC recovers all to input · sends FIFO when the current response finishes',
  pendingCount: 'Pending {count}/{max}',
  pendingClearAllAria: 'Clear all pending messages',
  pendingClearAll: 'Clear all',
  pendingAttachmentOnly: '(attachment only)',
  pendingRemove: 'Remove',

  // Toolbar.
  toolbarExecutionMode: 'Execution mode',
  toolbarPilotRouter: 'Pilot Router',
  toolbarVisualEffects: 'Visual effects',
  toolbarUsageTitle: 'Session usage',
  toolbarModel: 'Model',
  toolbarModelUnreported: 'Not reported',
  toolbarOut: 'out',
  toolbarCost: 'cost',
  toolbarNoUsage: 'No usage yet',
  bypassConfirmTitle: 'Enable approval bypass?',
  bypassConfirmBody:
    'This allows host execution without approval prompts in this browser session. This maps to /elevated bypass.',
  bypassConfirmNote: 'Sensitive-path checks remain active.',
  bypassConfirmAction: 'Enable bypass',

  // Elevated-mode pill (chat.js:2314-2343).
  pillUnavailable: 'Bypass N/A',
  pillTitleUnavailable:
    'Bypass requires an admitted Control connection. Reconnect with the configured Control token.',
  pillSession: 'Session {mode}',
  pillTitleSession:
    'Session permission override is active. Approval prompts are bypassed for this browser chat session. Click to clear the override.',
  pillGlobal: 'Global {mode}',
  pillTitleGlobal:
    'Global permission default controls execution mode and is configured by agentos sandbox on|bypass|full|reset.',
  pillNeutral: 'Approval prompts',
  pillTitleNeutral:
    'Approval prompts are active. Click to enable approval bypass for this browser session.',

  // Slash menu.
  slashLandmark: 'Slash commands',

  // Session chip.
  sessionLabel: 'session',
  sessionSwitchAria: 'Switch chat session',
  sessionActions: 'Chat actions',
  sessionCopyKeyAria: 'Copy session key',
  sessionCopyKey: 'Copy session key',
  sessionResetAria: 'Reset session',
  sessionReset: 'Reset session',
  sessionExportAria: 'Export chat as Markdown',
  sessionExport: 'Export Markdown',
  sessionRenameAria: 'Rename session',
  sessionRename: 'Rename session',
  sessionRenameInput: 'Session name',
  sessionRenamePlaceholder: 'Add a name',
  sessionRenameHint: 'Enter to save · Esc to cancel',
  sessionRenamed: 'Session renamed',
  sessionRenameCleared: 'Session name cleared',
  sessionRenameFailed: 'Rename failed: {message}',
  sessionMoveToProjectAria: 'Move this session to a project',
  sessionMoveToProject: 'Move to project',
  sessionMoveNoProject: 'No project',
  sessionMoved: 'Session moved to project',
  sessionMoveDetached: 'Session removed from project',
  sessionMoveFailed: 'Move failed: {message}',
  sessionSwitchDialog: 'Switch session',
  sessionKeyPlaceholder: 'Enter session key...',
  sessionKeyLabel: 'Session key',
  sessionListUnavailable: 'Session list unavailable. Enter a key above.',
  sessionSwitchTyped: 'Switch to typed session',
  sessionSearchPlaceholder: 'Search name or key…',
  sessionSearchLabel: 'Search sessions by name or key',
  sessionLoading: 'Loading…',
  sessionNoMatches: 'No matches.',
  sessionNoSessions: 'No sessions found.',
  sessionCurrent: 'current',

  // Session groups — the SessionGroup union stays a stable token set (it is a
  // type and a bucket key); only the rendered label is translated.
  groupWebChat: 'Web chat',
  groupCli: 'CLI',
  groupSubagents: 'Sub-agents',
  groupAgents: 'Agents',
  groupSessions: 'Sessions',
  groupOther: 'Other',

  // Projects integration.
  projectBadgeTitle: 'This session is in project "{name}" and shares its knowledge.',

  // Run status labels (logic.ts).
  runQueued: 'Queued',
  runRunning: 'Running',
  runApprovalPending: 'Waiting for approval',
  runInterrupted: 'Interrupted',
  runFailed: 'Failed',
  runTimeout: 'Timed out',
  runCancelled: 'Cancelled',
  runIdle: 'Idle',

  // Day separators + send button (logic.ts).
  dayToday: 'Today',
  dayYesterday: 'Yesterday',
  sendQueuedCompaction: 'Send (queues until compaction finishes)',
  sendQueuedBusy: 'Send (queues for after current response)',
  sendLabel: 'Send',

  // Markdown code blocks.
  copyCode: 'Copy code',
  mathTitle: 'LaTeX formula (not rendered)',
  copyFailed: 'Copy failed. Select the code manually.',
  externalImageBlocked: 'External image not loaded:',
  externalImageTitle:
    'Loading it would tell that host about this conversation, so it is a link, not a picture. Open it if you trust it.',

  // Session reset.
  resetNoBackup: 'Session reset without transcript backup',
  resetNoBackupDenied: 'Reset without backup requires an admitted Control connection.',
  resetFailed: 'Reset failed: {message}',
  resetDone: 'Session reset',
  resetBackupUnavailable: 'Transcript backup is unavailable.',
  resetBackupPrompt: 'Discard the current transcript and reset this session?',
  resetDiscardAction: 'Discard & reset',

  // Slash commands.
  slashNewChatHint: 'New chat is available from the session menu',
  slashCompactionRequested: 'Context compaction requested',
  slashCompactionFailed: 'Compaction failed: {message}',
  slashUsageHint: 'Usage page is available from the sidebar',
  slashUsageUnavailable: 'Usage cost unavailable',
  slashUsageFailed: 'Usage failed: {message}',
  slashNoModelsMatch: 'No models match "{filter}"',
  slashNoModels: 'No models available',
  slashModelListFailed: 'Model list failed: {message}',
  slashRouterPinned: 'Router pinned to {target}',
  slashRouterPinFailed: 'Router pin failed: {message}',
  slashRoutingRestored: 'Automatic routing restored',
  slashRoutingAlready: 'Automatic routing already active',
  slashRouterUnpinFailed: 'Router unpin failed: {message}',
  slashUseNeedsModel: 'Usage: /use <model-id>',
  slashUnsupported: 'Unsupported command: {command}',

  // Composer route picker.
  routeLabel: 'Model route',
  routeAuto: 'Auto',
  routeAutoHint: 'Let the Pilot Router choose',
  routeAutoWithTier: 'Auto · {tier}',
  routeAutoWithTierModel: 'Auto · {tier} · {model}',
  routeAutoTitle: 'The Pilot Router picks a tier for each turn',
  routeAutoRoutedTitle: 'The Pilot Router routed the last turn to {tier} · {model}',
  routePinned: 'Pinned to {target}',
  routePinnedTitle: 'Pinned to {tier} until you change it',
  routeModelPinnedTitle: 'Pinned to {model} until you change it',
  routeSearchPlaceholder: 'Search tiers and models',
  routeNoMatch: 'No route matches',
  routeDisabledTitle: 'Turn on the Pilot Router to pick a tier',
  routeImageHint: 'Images route here automatically',
  routeImageHintTitle:
    'Image turns are routed to a vision tier before any pin is applied, so these cannot be pinned',
  routeImageOverride: 'image route',
  routeImageOverrideTitle:
    'This turn used the image tier: image turns are routed before the pin is applied',

  // Transcript wiring.
  attachmentsPrompt: 'Describe these attachments',
  sendFailed: 'Send failed: {message}',
  historyLoadFailed: 'Could not load chat history.',
  historyEarlierFailed: 'Could not load earlier history.',
  noSubscriptionManager: 'No subscription manager available',
  streamMissedEvents: 'Missed live stream events; transcript refreshed.',
  streamSubscribeFailed: 'Session stream subscription failed: {message}',
  capWarning: 'Cap warning',
  taskTimedOut: 'The task timed out before it could finish.',
  taskStopped: 'The task stopped before it could finish.',
  taskCancelled: 'The task was cancelled before it finished.',
  agentError: 'Agent error',
  streamGap: 'Stream connection gap detected; reconnecting.',

  // Transcript: artifacts + charts + cards.
  artifactDownload: 'Download',
  artifactDownloadTitle: 'Download {name}',
  // The file's kind, for a card subtitle ("Spreadsheet · XLSX · 5 KB").
  artifactKindSpreadsheet: 'Spreadsheet',
  artifactKindDocument: 'Document',
  artifactKindPdf: 'PDF',
  artifactKindPresentation: 'Presentation',
  artifactKindImage: 'Image',
  artifactKindAudio: 'Audio',
  artifactKindArchive: 'Archive',
  artifactKindCode: 'Code',
  artifactKindData: 'Data',
  artifactKindText: 'Text',
  artifactKindWeb: 'Web page',
  artifactKindFile: 'File',
  chartLoading: 'Loading chart…',
  chartUnavailable: 'Chart data is unavailable.',
  chartUnreadable: 'Chart data could not be read.',
  chartFailed: 'Chart failed to load.',
  cardsLoading: 'Loading results…',
  cardsUnavailable: 'Result data is unavailable.',
  cardsUnreadable: 'Result data could not be read.',
  cardsFailed: 'Results failed to load.',
  cardsCopy: 'Copy',
  cardsOverflow: '+{count} more not shown',

  // Transcript: history paging.
  historyLoadingEarlier: 'Loading earlier messages...',
  historyOlderAvailable: 'Older history is available.',
  historyCompacted: 'Older context was compacted for the model.',
  historyExportHint: 'Export the session for exact text.',
  historyLoadEarlier: 'Load earlier',
  historyRetry: 'Retry',
  historyRetryHistory: 'Retry history',
  historyEmpty: 'No messages yet.',

  // Transcript: message actions.
  msgCronTag: 'Cron',
  msgSubagentCompletion: 'Subagent completion',
  msgUserActions: 'User message actions',
  msgAgentActions: 'Agent message actions',
  msgCopy: 'Copy message',
  msgRegenerate: 'Regenerate response',
  msgEdit: 'Edit message',
  msgCopied: 'Copied',
  msgCopyFailedError: 'Copy failed',
  msgCopyFailed: 'Copy failed: {message}',
  msgWaitForResponse: 'Wait for the current response to finish',
  msgNoPrevious: 'No previous message to regenerate',

  // Transcript: thinking indicator verbs (chat.js:381-382).
  verbWatching: 'Watching',
  verbTracking: 'Tracking',
  verbSensing: 'Sensing',
  verbPulsing: 'Pulsing',
  verbThinking: 'Thinking',
  verbDrafting: 'Drafting',
  verbPolishing: 'Polishing',
  stillWaiting: 'Still waiting for agent response…',

  // Transcript: collapsible model-reasoning block.
  thinkingBlockLabel: 'Thinking',
  thinkingBlockLoading: 'Loading reasoning…',
  thinkingBlockEmpty: 'No reasoning recorded for this reply.',
  thinkingBlockError: 'Failed to load reasoning. Expand again to retry.',

  // Transcript: tool cards.
  toolRunning: 'Running',
  toolCompleted: 'Completed',
  toolFailed: 'Failed',
  toolUnknownStatus: 'Unknown status',
  toolSearchProvider: 'Search provider: {provider}',
  toolViewFull: 'View full',
  toolResultTitle: 'Tool Result',

  // ask_user question card.
  askCardLabel: 'The agent is asking you',
  askSend: 'Send answer',
  askOtherPlaceholder: 'Or type your own answer…',
  askMultiHint: 'Choose one or more',
  askAnswered: 'Answered',

  // exit_plan_mode plan card + toolbar row.
  planCardLabel: 'Proposed plan',
  planApprove: 'Approve plan',
  planApproved: 'Plan approved — implementation starting',
  planRefineHint: 'or keep chatting to refine the plan',
  planApprovedMessage: 'Plan approved — proceed with the implementation.',
  toolbarPlanMode: 'Plan mode',

  // Transcript: router visualization.
  routerChoosing: 'Choosing a model',
  routerSuggested: 'Suggested model',
  routerSelected: 'Model selected',
  routerFinalizing: 'Finalizing model',

  // Transcript: compaction.
  compactSkipCoverage:
    'Context was left unchanged because required details could not be preserved.',
  compactSkipEmptyEphemeral: 'No compactable chat history yet.',
  compactSkipEmptySummary: 'Context was left unchanged because no usable summary was produced.',
  compactSkipNoEntries: 'No compactable chat history yet.',
  compactSkipNoBoundary: 'Context cannot be compacted safely during the current tool turn.',
  compactDetailCoverage: 'Required details could not be preserved',
  compactDetailEmptyEphemeral: 'No compactable history',
  compactDetailEmptySummary: 'No usable summary was produced',
  compactDetailNoEntries: 'No compactable history',
  compactDetailNoBoundary: 'Current tool turn boundary is not safe to compact',
  compactDetailUnsafeFlush: 'Memory safety check did not complete',
  compactWithinBudget: 'Already within context budget; no compact was applied.',
  compactCouldNotApply: 'Context compaction could not be applied',
  compactSkipped: 'Context compaction skipped',
  compactEphemeralDetail: 'Request-scoped; session history was not rewritten',
  compactMemoryOrganizing: 'Memory saved; organizing',
  compactTemporaryToast: 'Continuing with temporary context compaction for this turn',
  compactSeparatorDone: 'context compacted',
  compactSeparatorFailed: 'compaction failed',
  compactFailedToast: 'Compact failed{detail}{pending}',
  compactPendingPreserved: '; pending message preserved',
  compactPendingRecovered: '; pending message recovered to input',
  compactCancelledToast: 'Compact cancelled{pending}',
} as const)
