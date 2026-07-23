import { describe, it, expect } from 'vitest'
import {
  compactionTerminalStatus,
  compactionSeparatorTone,
  compactionStatusLabel,
  shouldPersistCompactionSeparator,
  compactionSeparatorAnimated,
  compactionUserVisible,
  compactionReason,
  compactionSkipMessage,
  compactionStatusDetail,
  compactFailureBlocksPending,
  compactSemanticMemoryNotice,
  compactSafeMessageDetail,
  createCompactionToastDedup,
  INTERNAL_COMPACTION_SKIP_REASONS,
} from './compaction'

/* ── compactionTerminalStatus (parity chat.js:3000) ─────────────────────── */

describe('compactionTerminalStatus (parity chat.js:2991-3002)', () => {
  it('is true for the six terminal statuses (case-insensitive)', () => {
    for (const s of [
      'completed',
      'skipped',
      'failed',
      'error',
      'cancelled',
      'emergency_ephemeral',
    ]) {
      expect(compactionTerminalStatus(s)).toBe(true)
      expect(compactionTerminalStatus(s.toUpperCase())).toBe(true)
    }
  })
  it('is false for non-terminal / unknown statuses (started, observed, done)', () => {
    expect(compactionTerminalStatus('started')).toBe(false)
    expect(compactionTerminalStatus('observed')).toBe(false)
    expect(compactionTerminalStatus('done')).toBe(false)
    expect(compactionTerminalStatus('running')).toBe(false)
    expect(compactionTerminalStatus('')).toBe(false)
    expect(compactionTerminalStatus(undefined)).toBe(false)
  })
})

/* ── compactionSeparatorTone (parity chat.js:3035) ──────────────────────── */

describe('compactionSeparatorTone (parity chat.js:3035-3041)', () => {
  it('maps completed → ok', () => {
    expect(compactionSeparatorTone('completed')).toBe('ok')
  })
  it('maps failed/error → err (NOT "error")', () => {
    expect(compactionSeparatorTone('failed')).toBe('err')
    expect(compactionSeparatorTone('error')).toBe('err')
  })
  it('maps cancelled / emergency_ephemeral → warn', () => {
    expect(compactionSeparatorTone('cancelled')).toBe('warn')
    expect(compactionSeparatorTone('emergency_ephemeral')).toBe('warn')
  })
  it('maps skipped WITH a reason → warn, WITHOUT a reason → info', () => {
    expect(compactionSeparatorTone('skipped', { reason: 'coverage_blocked' })).toBe('warn')
    expect(compactionSeparatorTone('skipped', {})).toBe('info')
  })
  it('falls back to info for started/observed/unknown', () => {
    expect(compactionSeparatorTone('started')).toBe('info')
    expect(compactionSeparatorTone('observed')).toBe('info')
    expect(compactionSeparatorTone('whatever')).toBe('info')
  })
})

/* ── compactionStatusLabel (parity chat.js:3019) ────────────────────────── */

describe('compactionStatusLabel (parity chat.js:3019-3033)', () => {
  it('started/observed → "context compacting"', () => {
    expect(compactionStatusLabel({}, '', 'started')).toBe('context compacting')
    expect(compactionStatusLabel({}, '', 'observed')).toBe('context compacting')
  })
  it('emergency_ephemeral → "temporary compaction"', () => {
    expect(compactionStatusLabel({}, '', 'emergency_ephemeral')).toBe('temporary compaction')
  })
  it('skipped with no reason / an internal reason → "no compaction needed"', () => {
    expect(compactionStatusLabel({}, '', 'skipped')).toBe('no compaction needed')
    expect(compactionStatusLabel({ reason: 'no_entries' }, '', 'skipped')).toBe(
      'no compaction needed',
    )
  })
  it('skipped with an external reason → "compaction skipped"', () => {
    expect(compactionStatusLabel({ reason: 'coverage_blocked' }, '', 'skipped')).toBe(
      'compaction skipped',
    )
  })
  it('failed/error → "compaction failed"; cancelled → "compaction cancelled"; completed → "context compacted"', () => {
    expect(compactionStatusLabel({}, '', 'failed')).toBe('compaction failed')
    expect(compactionStatusLabel({}, '', 'error')).toBe('compaction failed')
    expect(compactionStatusLabel({}, '', 'cancelled')).toBe('compaction cancelled')
    expect(compactionStatusLabel({}, '', 'completed')).toBe('context compacted')
  })
  it('unknown status → source-dependent default', () => {
    expect(compactionStatusLabel({}, 'manual', 'weird')).toBe('manual compact')
    expect(compactionStatusLabel({}, 'auto', 'weird')).toBe('context maintenance')
  })
})

/* ── shouldPersistCompactionSeparator (parity chat.js:3011) ─────────────── */

describe('shouldPersistCompactionSeparator (parity chat.js:3011-3017)', () => {
  it('honours an explicit persist override', () => {
    expect(shouldPersistCompactionSeparator('started', '', { persist: true })).toBe(true)
    expect(shouldPersistCompactionSeparator('completed', '', { persist: false })).toBe(false)
  })
  it('persists ONLY completed among terminal statuses', () => {
    expect(shouldPersistCompactionSeparator('completed', '')).toBe(true)
    expect(shouldPersistCompactionSeparator('skipped', '')).toBe(false)
    expect(shouldPersistCompactionSeparator('failed', '')).toBe(false)
    expect(shouldPersistCompactionSeparator('cancelled', '')).toBe(false)
  })
  it('non-terminal statuses never persist', () => {
    expect(shouldPersistCompactionSeparator('started', '')).toBe(false)
    expect(shouldPersistCompactionSeparator('observed', '')).toBe(false)
  })
})

/* ── compactionSeparatorAnimated (parity chat.js:3004) ──────────────────── */

describe('compactionSeparatorAnimated (parity chat.js:3004-3009)', () => {
  it('honours an explicit animated override', () => {
    expect(compactionSeparatorAnimated('completed', { animated: true })).toBe(true)
    expect(compactionSeparatorAnimated('started', { animated: false })).toBe(false)
  })
  it('animates only started/observed by default', () => {
    expect(compactionSeparatorAnimated('started')).toBe(true)
    expect(compactionSeparatorAnimated('observed')).toBe(true)
    expect(compactionSeparatorAnimated('completed')).toBe(false)
    expect(compactionSeparatorAnimated('skipped')).toBe(false)
  })
})

/* ── compactionUserVisible (parity chat.js:3231) ────────────────────────── */

describe('compactionUserVisible (parity chat.js:3231-3241)', () => {
  it('honours an explicit user_visible flag', () => {
    expect(compactionUserVisible({ user_visible: false }, 'manual', 'started')).toBe(false)
    expect(compactionUserVisible({ user_visible: true }, 'auto', 'skipped')).toBe(true)
  })
  it('manual source is always visible', () => {
    expect(compactionUserVisible({ reason: 'no_entries' }, 'manual', 'skipped')).toBe(true)
  })
  it('skipped with an internal reason is hidden; with an external reason is visible', () => {
    expect(compactionUserVisible({ reason: 'within_budget' }, '', 'skipped')).toBe(false)
    expect(compactionUserVisible({ reason: 'coverage_blocked' }, '', 'skipped')).toBe(true)
  })
  it('non-skipped auto statuses are visible', () => {
    expect(compactionUserVisible({}, '', 'started')).toBe(true)
    expect(compactionUserVisible({}, '', 'completed')).toBe(true)
  })
})

/* ── compactionReason (parity chat.js:3227) ─────────────────────────────── */

describe('compactionReason (parity chat.js:3227-3229)', () => {
  it('prefers reason, then skip_reason, else ""', () => {
    expect(compactionReason({ reason: 'a' })).toBe('a')
    expect(compactionReason({ skip_reason: 'b' })).toBe('b')
    expect(compactionReason({})).toBe('')
    expect(compactionReason(null)).toBe('')
  })
})

/* ── compactionSkipMessage (parity chat.js:3243) ────────────────────────── */

describe('compactionSkipMessage (parity chat.js:3243-3251)', () => {
  it('manual: known reason → its message; unknown → budget default', () => {
    expect(compactionSkipMessage({ reason: 'no_entries' }, 'manual')).toBe(
      'No compactable chat history yet.',
    )
    expect(compactionSkipMessage({ reason: 'xyz' }, 'manual')).toBe(
      'Already within context budget; no compact was applied.',
    )
  })
  it('auto: known reason → "could not be applied"; other reason → "skipped"; none → budget default', () => {
    expect(compactionSkipMessage({ reason: 'coverage_blocked' }, 'auto')).toBe(
      'Context compaction could not be applied',
    )
    expect(compactionSkipMessage({ reason: 'some_other' }, 'auto')).toBe(
      'Context compaction skipped',
    )
    expect(compactionSkipMessage({}, 'auto')).toBe(
      'Already within context budget; no compact was applied.',
    )
  })
})

/* ── compactionStatusDetail (parity chat.js:3253) ───────────────────────── */

describe('compactionStatusDetail (parity chat.js:3253-3261)', () => {
  it('returns "" when not user-visible', () => {
    expect(compactionStatusDetail({ reason: 'within_budget' }, '', 'skipped')).toBe('')
  })
  it('emergency_ephemeral → request-scoped detail', () => {
    expect(compactionStatusDetail({}, '', 'emergency_ephemeral')).toBe(
      'Request-scoped; session history was not rewritten',
    )
  })
  it('internal skip reason → "" (checked BEFORE the detail map, chat.js:3257)', () => {
    expect(compactionStatusDetail({ reason: 'no_entries' }, 'manual', 'skipped')).toBe('')
  })
  it('mapped external reason → its detail; unmapped reason → underscore-spaced', () => {
    expect(compactionStatusDetail({ reason: 'coverage_blocked' }, 'manual', 'skipped')).toBe(
      'Required details could not be preserved',
    )
    expect(compactionStatusDetail({ reason: 'custom_thing' }, 'manual', 'skipped')).toBe(
      'custom thing',
    )
  })
})

/* ── compactFailureBlocksPending (parity chat.js:3158) ──────────────────── */

describe('compactFailureBlocksPending (parity chat.js:3158-3178)', () => {
  it('blocks on refused / safe_to_send false', () => {
    expect(compactFailureBlocksPending({ refused: true })).toBe(true)
    expect(compactFailureBlocksPending({ safe_to_send: false })).toBe(true)
    expect(compactFailureBlocksPending({ safeToSend: false })).toBe(true)
  })
  it('blocks on a blocking error reason (any of the four)', () => {
    expect(compactFailureBlocksPending({ reason: 'context_overflow' })).toBe(true)
    expect(compactFailureBlocksPending({ error: { code: 'unsafe_flush_receipt' } })).toBe(true)
  })
  it('does not block otherwise', () => {
    expect(compactFailureBlocksPending({ reason: 'other' })).toBe(false)
    expect(compactFailureBlocksPending(null)).toBe(false)
  })
})

/* ── compactSemanticMemoryNotice (parity chat.js:3180) ──────────────────── */

describe('compactSemanticMemoryNotice (parity chat.js:3180-3189)', () => {
  it('returns the notice when semantic memory is degraded and safety is not error', () => {
    expect(compactSemanticMemoryNotice({ semanticMemory: { status: 'degraded' } })).toBe(
      'Memory saved; organizing',
    )
    expect(compactSemanticMemoryNotice({ semantic_memory: { status: 'DEGRADED' } })).toBe(
      'Memory saved; organizing',
    )
  })
  it('returns "" when safety is error, or semantic is not degraded', () => {
    expect(
      compactSemanticMemoryNotice({
        semanticMemory: { status: 'degraded' },
        memorySafety: { status: 'error' },
      }),
    ).toBe('')
    expect(compactSemanticMemoryNotice({ semanticMemory: { status: 'ok' } })).toBe('')
    expect(compactSemanticMemoryNotice({})).toBe('')
  })
})

/* ── compactSafeMessageDetail (parity chat.js:3191) ─────────────────────── */

describe('compactSafeMessageDetail (parity chat.js:3191-3198)', () => {
  it('redacts checkpoint paths to "[memory checkpoint]"', () => {
    expect(compactSafeMessageDetail({ message: 'failed at /var/checkpoint-42/x' })).toBe(
      'failed at [memory checkpoint]',
    )
    expect(compactSafeMessageDetail({ message: 'lost memory/.raw_fallbacks/abc123 here' })).toBe(
      'lost [memory checkpoint] here',
    )
  })
  it('returns "" when no message', () => {
    expect(compactSafeMessageDetail({})).toBe('')
  })
})

/* ── createCompactionToastDedup (parity chat.js:2916-2928) ──────────────── */

describe('createCompactionToastDedup (parity chat.js:2916-2928)', () => {
  it('suppresses an identical signature within 1500ms, then allows a distinct one', () => {
    let now = 1000
    const dedup = createCompactionToastDedup({
      now: () => now,
      getSessionKey: () => 's1',
    })
    expect(dedup.suppress({ key: 's1' }, 'completed', 'manual')).toBe(false)
    now = 1200
    expect(dedup.suppress({ key: 's1' }, 'completed', 'manual')).toBe(true) // dup within window
    now = 1400
    expect(dedup.suppress({ key: 's1' }, 'skipped', 'manual')).toBe(false) // distinct sig
  })
  it('allows a repeat after the 1500ms window elapses', () => {
    let now = 1000
    const dedup = createCompactionToastDedup({ now: () => now, getSessionKey: () => 's1' })
    expect(dedup.suppress({ key: 's1' }, 'completed', 'auto')).toBe(false)
    now = 3000
    expect(dedup.suppress({ key: 's1' }, 'completed', 'auto')).toBe(false)
  })
})

/* ── INTERNAL_COMPACTION_SKIP_REASONS (parity chat.js:3200-3208) ────────── */

describe('INTERNAL_COMPACTION_SKIP_REASONS (parity chat.js:3200-3208)', () => {
  it('contains the seven internal reasons', () => {
    for (const r of [
      'already_attempted_this_turn',
      'already_compacted_this_turn',
      'no_entries',
      'stale_preimage',
      'structured_content_noop',
      'within_budget',
      'within_compaction_budget',
    ]) {
      expect(INTERNAL_COMPACTION_SKIP_REASONS.has(r)).toBe(true)
    }
  })
})
