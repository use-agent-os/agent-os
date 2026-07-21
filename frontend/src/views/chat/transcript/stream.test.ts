import { describe, it, expect } from 'vitest'
import { createSeqGate } from './stream'

// The seq gate is the one pure, timing-independent core of the streaming
// renderer. It is ported verbatim from legacy chat.js
// (_markSessionStreamSeqSeen / _sessionStreamSeq / _setSessionStreamSeq /
// _sessionStreamSeqSeen, chat.js:1645-1682) which _acceptStreamSeq
// (chat.js:6345-6350) drives. The DOM-mutation side of the renderer is
// verified by a live-browser sweep (see the parity matrix), NOT here.

describe('stream seq gate (parity chat.js:6345-6378, 1645-1682)', () => {
  it('accepts strictly increasing seqs and rejects duplicates within the 800 window', () => {
    const gate = createSeqGate()
    expect(gate.accept('s1', 1)).toBe(true)
    expect(gate.accept('s1', 1)).toBe(false) // duplicate
    expect(gate.accept('s1', 2)).toBe(true)
  })

  it('tracks seqs per session independently', () => {
    const gate = createSeqGate()
    expect(gate.accept('s1', 5)).toBe(true)
    expect(gate.accept('s2', 5)).toBe(true) // different session
  })

  it('accepts out-of-order (lower-than-highwater) seqs that have not been seen', () => {
    // Legacy dedupes on a Set membership, not on a monotonic counter: a lower
    // seq that was never seen is still accepted (chat.js:1670 seen.has check).
    const gate = createSeqGate()
    expect(gate.accept('s1', 10)).toBe(true)
    expect(gate.accept('s1', 3)).toBe(true) // never seen, below high-water → accept
    expect(gate.accept('s1', 3)).toBe(false) // now seen → reject
  })

  it('treats non-finite / non-number seqs and empty keys as pass-through (returns true)', () => {
    // chat.js:1668 — a missing key or a non-finite seq is not gated.
    const gate = createSeqGate()
    expect(gate.accept('', 1)).toBe(true)
    expect(gate.accept('', 1)).toBe(true) // empty key never dedupes
    expect(gate.accept('s1', Number.NaN)).toBe(true)
    expect(gate.accept('s1', Number.POSITIVE_INFINITY)).toBe(true)
  })

  it('prunes seen entries below (high-water - 800) once the window is exceeded so an old seq can re-enter', () => {
    // chat.js:1674-1680 — after the seen set exceeds the 800 window it drops
    // every value below (highWater - 800). A seq that predates that cutoff is
    // pruned, so the same low seq is accepted again (the seen memory is bounded).
    const gate = createSeqGate()
    // Seed a low seq, then push the high-water far past it plus fill the window.
    expect(gate.accept('s1', 1)).toBe(true)
    for (let seq = 1000; seq < 1000 + 800; seq += 1) {
      expect(gate.accept('s1', seq)).toBe(true)
    }
    // seen.size is now > 800; high-water = 1799, cutoff = 999. seq 1 < 999 → pruned.
    // Re-presenting seq 1 is accepted again (it was forgotten).
    expect(gate.accept('s1', 1)).toBe(true)
  })

  it('exposes the high-water sequence per session (parity _sessionStreamSeq)', () => {
    const gate = createSeqGate()
    gate.accept('s1', 4)
    gate.accept('s1', 9)
    gate.accept('s1', 7)
    expect(gate.highWater('s1')).toBe(9) // Math.max of accepted seqs
    expect(gate.highWater('s2')).toBe(0) // untouched session
    gate.sync('s2', 12)
    expect(gate.highWater('s2')).toBe(12) // server-advertised subscribe cursor
  })
})
