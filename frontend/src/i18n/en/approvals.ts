import { defineNamespace } from '../registry'

export const approvals = defineNamespace('approvals', {
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
} as const)
