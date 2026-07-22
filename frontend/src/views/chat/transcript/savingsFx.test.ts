import { beforeEach, describe, expect, it } from 'vitest'
import {
  SAVINGS_FX_PREF_KEY,
  SAVINGS_POPUP_COOLDOWN_MS,
  createSavingsFx,
  isSavingsComboTier,
  savingsLabel,
  savingsScore,
  savingsTurnIdentity,
  type SavingsUsage,
} from './savingsFx'

function routed(model = 'provider/fast', tier = 'c1'): SavingsUsage {
  return {
    model,
    routed_model: model,
    routed_tier: tier,
    routing_source: 'pilot',
    total_savings_pct: 42,
  }
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
})

describe('SavingsFX pure helpers', () => {
  it('formats only a real supplied percentage', () => {
    expect(savingsLabel(0)).toBe('Cost optimized')
    expect(savingsLabel(0.9)).toBe('Cost optimized')
    expect(savingsLabel(1)).toBe('Saved ~1%')
    expect(savingsLabel(64.6)).toBe('Saved ~65%')
    expect(savingsLabel(undefined)).toBe('Cost optimized')
  })

  it('uses exact model+tier identities and excludes top numeric tiers from combos', () => {
    expect(savingsTurnIdentity(routed('provider/model', 'c2'))).toBe('provider/model|c2')
    expect(savingsTurnIdentity({ routed_tier: 'c1' })).toBe('')
    expect(isSavingsComboTier('c1')).toBe(true)
    expect(isSavingsComboTier('t2')).toBe(true)
    expect(isSavingsComboTier('c3')).toBe(false)
    expect(isSavingsComboTier('t10')).toBe(false)
    expect(isSavingsComboTier('flagship')).toBe(false)
    expect(isSavingsComboTier('fast')).toBe(true)
  })

  it('applies the legacy USD/pct/confidence weights and clamps to 0.25..1', () => {
    expect(savingsScore({})).toBe(0.25)
    expect(
      savingsScore({
        total_savings_usd: 0.025,
        total_savings_pct: 50,
        routing_confidence: 0.8,
      }),
    ).toBeCloseTo(0.53)
    expect(
      savingsScore({
        total_savings_usd: 1,
        total_savings_pct: 200,
        routing_confidence: 1,
      }),
    ).toBe(1)
  })
})

describe('SavingsFX state', () => {
  it('increments only consecutive qualifying identities and preserves the maximum on reset', () => {
    const fx = createSavingsFx({ storage: null })
    fx.noteTurn(routed())
    fx.noteTurn(routed())
    expect(fx.getStreak()).toEqual({ current: 2, max: 2 })

    fx.noteTurn(routed('provider/other'))
    expect(fx.getStreak()).toEqual({ current: 1, max: 2 })
    fx.noteTurn({ cached_tokens: 20, cache_hit_active: true })
    expect(fx.getStreak()).toEqual({ current: 0, max: 2 })
    fx.resetStreak()
    expect(fx.getStreak()).toEqual({ current: 0, max: 2 })
  })

  it('defaults ON, reads exact "0", and persists exact string values', () => {
    const values = new Map<string, string>([[SAVINGS_FX_PREF_KEY, '0']])
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    }
    const fx = createSavingsFx({ storage })
    expect(fx.isEnabled()).toBe(false)
    fx.setEnabled(true)
    expect(values.get(SAVINGS_FX_PREF_KEY)).toBe('1')
    fx.setEnabled(false)
    expect(values.get(SAVINGS_FX_PREF_KEY)).toBe('0')
    expect(createSavingsFx({ storage: null }).isEnabled()).toBe(true)
  })

  it('keeps streak bookkeeping active while the production burst gate is off', () => {
    const fx = createSavingsFx({ storage: null })
    const first = routed()
    const switched = routed('provider/other')
    const continued = routed('provider/other')

    expect(fx.maybeFire(null, first)).toBe(false)
    expect(fx.getStreak().current).toBe(1)
    expect(fx.maybeFire(null, switched)).toBe(false)
    expect(switched.__savings_ui_suppressed).toBe(true)
    expect(fx.getStreak().current).toBe(1)
    expect(fx.maybeFire(null, continued)).toBe(false)
    expect(continued.__savings_ui_suppressed).toBeUndefined()
    expect(fx.getStreak().current).toBe(2)
    expect(document.querySelector('.savings-float')).toBeNull()
    expect(document.querySelector('canvas')).toBeNull()
  })

  it('applies per-identity cooldowns, cache bypass, and session reset when enabled for a test', () => {
    let now = SAVINGS_POPUP_COOLDOWN_MS + 1
    // Keep the visual preference off so this test exercises product gating state
    // without creating canvas/timer work in jsdom.
    const storage = {
      getItem: () => '0',
      setItem: () => {},
    }
    const fx = createSavingsFx({ storage, burstEnabled: true, now: () => now })
    const usage = routed()
    expect(fx.maybeFire(null, usage)).toBe(true)
    now += 100
    expect(fx.maybeFire(null, routed())).toBe(false)
    expect(fx.maybeFire(null, { ...routed(), cache_hit_active: true })).toBe(true)

    fx.resetPopupCooldown()
    expect(fx.getStreak().current).toBe(0)
    expect(fx.maybeFire(null, routed())).toBe(true)
  })

  it('recomputes a history streak oldest-to-newest without inflating on rerender', () => {
    const fx = createSavingsFx({ storage: null })
    fx.beginHistoryReplay()
    fx.noteHistoryTurn(routed())
    fx.noteHistoryTurn(routed())
    expect(fx.getStreak().current).toBe(2)

    fx.beginHistoryReplay()
    fx.noteHistoryTurn(routed())
    fx.noteHistoryTurn(null)
    expect(fx.getStreak().current).toBe(0)
  })
})
