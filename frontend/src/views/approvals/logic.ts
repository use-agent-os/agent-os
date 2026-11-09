// Pure approvals-view helpers ported 1:1 from the legacy view
// (src/agentos/gateway/static/js/views/approvals.js). Each function carries the
// legacy line range it mirrors so the parity matrix stays auditable. The
// pending-list / resolve / poll behaviors live in the Task-1 approval-monitor
// service (services/approval-monitor.ts); this module owns the config surface
// (approval-strategy options + effective-execution-mode summary).

import {
  normalizeElevatedMode,
  readBrowserElevated,
  type Approval,
  type ElevatedMode,
} from '@/services/approval-monitor'
// Registers this view's copy; it ships in this chunk, not the entry bundle.
import '@/i18n/en/approvals'
import { t } from '@/i18n'

// Re-exported from the single elevated-mode source of truth
// (services/approval-monitor.ts) so existing approvals-view imports of
// `normalizeElevatedMode` from this module keep resolving without a duplicate
// implementation. The chat toolbar shares the same source.
export { normalizeElevatedMode }

// (_approvalDetail) — the pending-card Details body: the
// warning text when present, else the FULL pretty-printed args/params JSON with
// NO length cap. This is deliberately distinct from the modal contract's
// approvalDetail (services/approval-monitor.ts), which truncates at 900 chars
// for the compact prompt; the legacy VIEW rendered args in full.
export function approvalCardDetail(item: Approval): string {
  if (item.warning) return String(item.warning)
  const args = item.args ?? item.params ?? null
  if (!args) return ''
  try {
    return JSON.stringify(args, null, 2)
  } catch {
    return String(args)
  }
}

export interface ModeOption {
  value: string
  label: string
  desc: string
}

// the three approval-strategy choices, in legacy order.
export function modeOptions(): ReadonlyArray<ModeOption> {
  return [
    {
      value: 'prompt',
      label: t('approvals.modePromptLabel'),
      desc: t('approvals.modePromptDesc'),
    },
    {
      value: 'auto-approve',
      label: t('approvals.modeAutoApproveLabel'),
      desc: t('approvals.modeAutoApproveDesc'),
    },
    {
      value: 'auto-deny',
      label: t('approvals.modeAutoDenyLabel'),
      desc: t('approvals.modeAutoDenyDesc'),
    },
  ]
}

// active option else the first (prompt) as the fallback.
export function activeModeOption(mode: string): ModeOption {
  const options = modeOptions()
  return options.find((m) => m.value === mode) || options[0]!
}

// strategy mode -> status tone. Legacy returned the
// legacy status-class names ('warn'/'err'/'ok'); rebuilt on the design-system
// --tone tokens: auto-deny is danger (was 'err'), auto-approve warn, else ok.
export type Tone = 'ok' | 'warn' | 'danger'
export function modeStateTone(mode: string): Tone {
  if (mode === 'auto-approve') return 'warn'
  if (mode === 'auto-deny') return 'danger'
  return 'ok'
}

// read the persisted browser elevated mode, downgrading a
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

// "<Scope> <MODE>" label + a scope/mode-specific blurb.
// `scope` is the DISPLAY word, so the session/global branch compares against the
// translated scope label rather than a hardcoded 'Session'. resolveExecutionMode
// below passes exactly these values, so the two stay in step in every locale.
export function executionModeSummary(scope: string, mode: string): ExecutionModeSummary {
  const label = t('approvals.execLabel', { scope, mode: String(mode).toUpperCase() })
  const isSession = scope === t('approvals.scopeSession')
  if (mode === 'bypass') {
    return {
      label,
      desc: isSession ? t('approvals.execBypassSession') : t('approvals.execBypassGlobal'),
    }
  }
  if (mode === 'full') {
    return {
      label,
      desc: isSession ? t('approvals.execFullSession') : t('approvals.execFullGlobal'),
    }
  }
  return { label, desc: t('approvals.execOn') }
}

/**
 * the pure derivation behind _loadExecutionModeSummary:
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
  if (session) return executionModeSummary(t('approvals.scopeSession'), session)
  const global = normalizeElevatedMode(globalDefaultMode)
  if (global) return executionModeSummary(t('approvals.scopeGlobal'), global)
  return {
    label: t('approvals.execNoneLabel'),
    desc: t('approvals.execNoneDesc'),
  }
}
