// Pure approvals-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/approvals.js). Each function carries the
// legacy line range it mirrors so the parity matrix stays auditable. The
// pending-list / resolve / poll behaviors live in the Task-1 approval-monitor
// service (services/approval-monitor.ts); this module owns the config surface
// (approval-strategy options + effective-execution-mode summary).

import { readBrowserElevated, type ElevatedMode } from '@/services/approval-monitor'

export interface ModeOption {
  value: string
  label: string
  desc: string
}

// approvals.js:82-86 — the three approval-strategy choices, in legacy order.
export const MODE_OPTIONS: ReadonlyArray<ModeOption> = [
  {
    value: 'prompt',
    label: 'Ask every time',
    desc: 'Every risky tool execution opens an approval prompt.',
  },
  {
    value: 'auto-approve',
    label: 'Auto approve',
    desc: 'All tool executions are automatically approved.',
  },
  {
    value: 'auto-deny',
    label: 'Auto deny',
    desc: 'All tool executions are automatically denied.',
  },
]

// approvals.js:87 — active option else the first (prompt) as the fallback.
export function activeModeOption(mode: string): ModeOption {
  return MODE_OPTIONS.find((m) => m.value === mode) || MODE_OPTIONS[0]!
}

// approvals.js:310-314 — strategy mode -> status tone. Legacy returned the
// legacy status-class names ('warn'/'err'/'ok'); rebuilt on the design-system
// --tone tokens: auto-deny is danger (was 'err'), auto-approve warn, else ok.
export type Tone = 'ok' | 'warn' | 'danger'
export function modeStateTone(mode: string): Tone {
  if (mode === 'auto-approve') return 'warn'
  if (mode === 'auto-deny') return 'danger'
  return 'ok'
}

// approvals.js:245-247 — only on/bypass/full are valid elevated modes.
export function normalizeElevatedMode(mode: string | null | undefined): ElevatedMode {
  return mode === 'on' || mode === 'bypass' || mode === 'full' ? mode : ''
}

// approvals.js:234-243 — read the persisted browser elevated mode, downgrading a
// legacy 'full' written under an older storage version to 'bypass'. Delegates to
// the service's readBrowserElevated (single source; the store hydrates from the
// same reader) so the localStorage read + version-downgrade live in one place.
export function browserElevatedMode(): ElevatedMode {
  return readBrowserElevated()
}

export interface ExecutionModeSummary {
  label: string
  desc: string
}

// approvals.js:194-216 — "<Scope> <MODE>" label + a scope/mode-specific blurb.
export function executionModeSummary(scope: string, mode: string): ExecutionModeSummary {
  const label = `${scope} ${String(mode).toUpperCase()}`
  if (mode === 'bypass') {
    return {
      label,
      desc:
        scope === 'Session'
          ? 'Approval prompts are currently bypassed for this browser chat session.'
          : 'Approval prompts are currently bypassed by the global permission mode.',
    }
  }
  if (mode === 'full') {
    return {
      label,
      desc:
        scope === 'Session'
          ? 'Approval and sensitive-path prompts are bypassed for this browser chat session.'
          : 'Approval and sensitive-path prompts are bypassed by the global permission mode.',
    }
  }
  return {
    label,
    desc: 'Host execution is enabled; risky tool calls still use approval prompts.',
  }
}

/**
 * approvals.js:176-192 — the pure derivation behind _loadExecutionModeSummary:
 * a browser session elevated mode wins ('Session'); else the normalized global
 * `permissions.default_mode` from config.get ('Global'); else the neutral
 * "Approval prompts" fallback. The async config.get fetch stays in the page (an
 * RPC read); this maps the two already-resolved inputs to the summary.
 */
export function resolveExecutionMode(
  sessionMode: string,
  globalDefaultMode: string,
): ExecutionModeSummary {
  const session = normalizeElevatedMode(sessionMode)
  if (session) return executionModeSummary('Session', session)
  const global = normalizeElevatedMode(globalDefaultMode)
  if (global) return executionModeSummary('Global', global)
  return {
    label: 'Approval prompts',
    desc: 'Risky tool calls will open approval prompts.',
  }
}
