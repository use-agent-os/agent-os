import { afterEach, describe, expect, it } from 'vitest'
import {
  MODE_OPTIONS,
  activeModeOption,
  approvalCardDetail,
  executionModeSummary,
  modeStateTone,
  normalizeElevatedMode,
  browserElevatedMode,
  resolveExecutionMode,
} from './logic'
import {
  ELEVATED_MODE_KEY,
  ELEVATED_MODE_VERSION_KEY,
  ELEVATED_MODE_STORAGE_VERSION,
} from '@/services/approval-monitor'

afterEach(() => {
  localStorage.clear()
})

describe('MODE_OPTIONS / activeModeOption', () => {
  it('lists prompt, auto-approve, auto-deny in legacy order (approvals.js:82-86)', () => {
    expect(MODE_OPTIONS.map((o) => o.value)).toEqual(['prompt', 'auto-approve', 'auto-deny'])
  })
  it('selects the option matching the active mode', () => {
    expect(activeModeOption('auto-deny').label).toBe('Auto deny')
  })
  it('falls back to the first option (prompt) for an unknown mode (approvals.js:87)', () => {
    expect(activeModeOption('nonsense')).toBe(MODE_OPTIONS[0])
    expect(activeModeOption('')).toBe(MODE_OPTIONS[0])
  })
})

describe('approvalCardDetail (approvals.js:314-322 — the VIEW, full args)', () => {
  it('returns the warning text when present', () => {
    expect(approvalCardDetail({ id: '1', warning: 'danger!' })).toBe('danger!')
  })
  it('pretty-prints args as full JSON', () => {
    expect(approvalCardDetail({ id: '1', args: { a: 1 } })).toBe('{\n  "a": 1\n}')
  })
  it('falls back to params when args is absent', () => {
    expect(approvalCardDetail({ id: '1', params: { b: 2 } })).toBe('{\n  "b": 2\n}')
  })
  it('renders the FULL args JSON with NO 900-char cap (unlike the modal contract)', () => {
    const big = { blob: 'x'.repeat(2000) }
    const out = approvalCardDetail({ id: '1', args: big })
    const expected = JSON.stringify(big, null, 2)
    expect(out).toBe(expected)
    expect(out.length).toBeGreaterThan(900)
    expect(out.endsWith('...')).toBe(false)
  })
  it('returns "" when there is neither warning nor args/params', () => {
    expect(approvalCardDetail({ id: '1' })).toBe('')
  })
})

describe('modeStateTone (approvals.js:310-314)', () => {
  it('maps auto-approve to warn', () => {
    expect(modeStateTone('auto-approve')).toBe('warn')
  })
  it('maps auto-deny to danger', () => {
    expect(modeStateTone('auto-deny')).toBe('danger')
  })
  it('maps prompt (and anything else) to ok', () => {
    expect(modeStateTone('prompt')).toBe('ok')
    expect(modeStateTone('other')).toBe('ok')
  })
})

describe('normalizeElevatedMode (approvals.js:245-247)', () => {
  it('passes on/bypass/full through', () => {
    expect(normalizeElevatedMode('on')).toBe('on')
    expect(normalizeElevatedMode('bypass')).toBe('bypass')
    expect(normalizeElevatedMode('full')).toBe('full')
  })
  it('clears anything else', () => {
    expect(normalizeElevatedMode('off')).toBe('')
    expect(normalizeElevatedMode('')).toBe('')
    expect(normalizeElevatedMode(null)).toBe('')
  })
})

describe('browserElevatedMode (approvals.js:234-243)', () => {
  it('returns the normalized stored mode', () => {
    localStorage.setItem(ELEVATED_MODE_KEY, 'bypass')
    localStorage.setItem(ELEVATED_MODE_VERSION_KEY, ELEVATED_MODE_STORAGE_VERSION)
    expect(browserElevatedMode()).toBe('bypass')
  })
  it('downgrades a legacy "full" stored under an old storage version to "bypass" (approvals.js:241)', () => {
    localStorage.setItem(ELEVATED_MODE_KEY, 'full')
    localStorage.setItem(ELEVATED_MODE_VERSION_KEY, '1')
    expect(browserElevatedMode()).toBe('bypass')
  })
  it('keeps "full" when stored under the current storage version', () => {
    localStorage.setItem(ELEVATED_MODE_KEY, 'full')
    localStorage.setItem(ELEVATED_MODE_VERSION_KEY, ELEVATED_MODE_STORAGE_VERSION)
    expect(browserElevatedMode()).toBe('full')
  })
  it('returns "" when nothing is stored', () => {
    expect(browserElevatedMode()).toBe('')
  })
})

describe('executionModeSummary (approvals.js:194-216)', () => {
  it('uppercases scope + mode into the label', () => {
    expect(executionModeSummary('Session', 'bypass').label).toBe('Session BYPASS')
    expect(executionModeSummary('Global', 'full').label).toBe('Global FULL')
  })
  it('describes a session bypass distinctly from a global bypass', () => {
    expect(executionModeSummary('Session', 'bypass').desc).toContain('browser chat session')
    expect(executionModeSummary('Global', 'bypass').desc).toContain('global permission mode')
  })
  it('describes full mode (approval + sensitive-path bypass)', () => {
    expect(executionModeSummary('Session', 'full').desc).toContain('sensitive-path')
  })
  it('falls back to the on/host-execution description for other modes', () => {
    expect(executionModeSummary('Session', 'on').desc).toContain('Host execution is enabled')
  })
})

describe('resolveExecutionMode (approvals.js:176-192)', () => {
  it('prefers the browser session elevated mode when present', () => {
    localStorage.setItem(ELEVATED_MODE_KEY, 'bypass')
    localStorage.setItem(ELEVATED_MODE_VERSION_KEY, ELEVATED_MODE_STORAGE_VERSION)
    const summary = resolveExecutionMode('bypass', 'prompt')
    expect(summary.label).toBe('Session BYPASS')
  })
  it('uses the global default_mode when no session mode is set', () => {
    const summary = resolveExecutionMode('', 'full')
    expect(summary.label).toBe('Global FULL')
  })
  it('falls back to the neutral "Approval prompts" summary when neither is set', () => {
    const summary = resolveExecutionMode('', '')
    expect(summary.label).toBe('Approval prompts')
    expect(summary.desc).toContain('approval prompts')
  })
  it('ignores an invalid global default_mode', () => {
    const summary = resolveExecutionMode('', 'garbage')
    expect(summary.label).toBe('Approval prompts')
  })
})
