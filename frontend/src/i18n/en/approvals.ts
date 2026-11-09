import { defineNamespace } from '../registry'

export const approvals = defineNamespace('approvals', {
  documentTitle: 'Approvals - AgentOS Control',
  eyebrow: 'Control · Approvals',
  title: 'Approvals',
  subtitle: 'Tool execution gate — keep risky actions paused until you say go.',
  refreshTitle: 'Refresh approvals',
  inboxLink: 'Attention inbox',
  inboxLinkTitle: 'Approvals, agent questions and runtime issues in one list',
  refresh: 'Refresh',
  refreshing: 'Refreshing…',

  // Approval-strategy options.
  modePromptLabel: 'Ask every time',
  modePromptDesc: 'Every risky tool execution opens an approval prompt.',
  modeAutoApproveLabel: 'Auto approve',
  modeAutoApproveDesc: 'All tool executions are automatically approved.',
  modeAutoDenyLabel: 'Auto deny',
  modeAutoDenyDesc: 'All tool executions are automatically denied.',

  // Effective execution mode.
  scopeSession: 'Session',
  scopeGlobal: 'Global',
  execLabel: '{scope} {mode}',
  execBypassSession: 'Approval prompts are currently bypassed for this browser chat session.',
  execBypassGlobal: 'Approval prompts are currently bypassed by the global permission mode.',
  execFullSession:
    'Approval and sensitive-path prompts are bypassed for this browser chat session.',
  execFullGlobal: 'Approval and sensitive-path prompts are bypassed by the global permission mode.',
  execOn: 'Host execution is enabled; risky tool calls still use approval prompts.',
  execNoneLabel: 'Approval prompts',
  execNoneDesc: 'Risky tool calls will open approval prompts.',

  // Pending approval card.
  cardUnknownTool: 'Unknown',
  cardLandmark: 'Approval request {tool}',
  cardAwaiting: 'awaiting decision',
  cardAgent: 'Agent',
  cardSession: 'Session',
  cardCommand: 'Command',
  cardDetails: 'Details',
  cardApproveOnce: 'Approve once',
  cardAlways: 'Always allow this type',
  cardBypassTitle: 'Bypass approval prompts while keeping sensitive-path checks',
  cardBypass: 'Bypass approvals',
  cardDeny: 'Deny',

  // Operations panel.
  opsLandmark: 'Approval operations',
  opsEyebrow: 'Execution gate',
  opsTitle: 'Decision posture',
  opsWaiting: '{count} waiting',
  opsQueueClear: 'Queue clear',
  statsLandmark: 'Approvals summary',
  statPending: 'Pending',
  statPendingHint: 'awaiting decision',
  statAllClear: 'all clear',
  statStrategy: 'Strategy',
  statExecution: 'Effective execution mode',

  // Queue.
  emptyTitle: 'No pending approvals.',
  emptyText: 'When an agent reaches a risky tool call, it will appear here for your sign-off.',
  pendingLandmark: 'Pending approvals',
  pendingTitle: 'Decision inbox',
  pendingSubtitle: 'Review the requested operation and choose the narrowest safe permission.',
  pendingCount: '{count} pending',

  // Policy panel.
  policyTitle: 'Approval policy',
  policySubtitle: 'Default response for future requests',
  policyLandmark: 'Approval strategy',

  // Toasts.
  toastStrategySaved: 'Approval strategy: {mode}',
  toastStrategyFailed: 'Failed to save strategy: {message}',
} as const)
