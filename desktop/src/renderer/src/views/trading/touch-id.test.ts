import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import { order, USDC } from './test-utils'
import {
  bracketAskById,
  bracketTouchId,
  mandateAskById,
  mandateCeilingUsd,
  mandateTouchId,
  manualTradeTouchId,
  orderTouchId,
  triggerAskById,
  triggerTouchId,
  vaultReason,
} from './touch-id'
import type {
  Bracket,
  BracketPayload,
  Mandate,
  MandatePayload,
  Trigger,
  TriggerPayload,
} from './types'

vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn() } }))

const MANDATE = (
  JSON.parse(
    readFileSync('src/renderer/src/views/trading/desk/__fixtures__/dca/mandate.json', 'utf8'),
  ) as MandatePayload
).mandate

function mandate(extra: Partial<Mandate> = {}): Mandate {
  return { ...MANDATE, ...extra }
}

const TRIGGER = (
  JSON.parse(
    readFileSync('src/renderer/src/views/trading/desk/__fixtures__/trigger/trigger.json', 'utf8'),
  ) as TriggerPayload
).trigger

describe('orderTouchId', () => {
  it('names a swap the way the card does, on its chain', () => {
    const ask = orderTouchId(order({ amountIn: '0.05', valueUsd: 120 }))
    expect(ask).toEqual({ kind: 'approve', reason: 'approve 0.05 ETH → USDC on Base' })
  })

  it('takes the card’s risk: value, impact, an unpriced send', () => {
    expect(orderTouchId(order({ valueUsd: 900 })).kind).toBe('approve-high')
    expect(orderTouchId(order({ valueUsd: 20, priceImpactPct: 12 })).kind).toBe('approve-high')
    const send = order({
      kind: 'send',
      tokenIn: USDC,
      amountIn: '25',
      valueUsd: null,
      recipient: '0x6c83a1b2c3d4e5f60718293a4b5c6d7e8f908312',
    })
    expect(orderTouchId(send)).toEqual({
      kind: 'approve-high',
      reason: 'approve send 25 USDC → 0x6c83…8312 on Base',
    })
  })

  it('reads a multisend as one ask: the batch total decides the risk', () => {
    const legs = [1, 2, 3].map((n) =>
      order({
        orderId: `o${n}`,
        kind: 'send',
        batchId: 'b1',
        tokenIn: USDC,
        amountIn: '200',
        valueUsd: 200,
        recipient: `0x${String(n).repeat(40)}`,
      }),
    )
    const ask = orderTouchId(legs[0]!, legs)
    expect(ask.kind).toBe('approve-high')
    expect(ask.reason).toBe('approve multisend 600 USDC → 3 recipients on Base')
    // One leg whose siblings are unknown is not taken for small.
    expect(orderTouchId({ ...legs[0]!, valueUsd: 1 }).kind).toBe('approve-high')
  })
})

describe('mandateTouchId', () => {
  it('reads what the mandate may spend: its cap, else buys × USD per buy', () => {
    expect(mandateCeilingUsd(mandate({ budget: { ...MANDATE.budget, capUsd: 120 } }))).toBe(120)
    expect(
      mandateCeilingUsd(
        mandate({
          budget: { ...MANDATE.budget, capUsd: 0, usdPerRun: 25 },
          runs: { ...MANDATE.runs, max: 4 },
        }),
      ),
    ).toBe(100)
    expect(
      mandateCeilingUsd(
        mandate({
          budget: { ...MANDATE.budget, capUsd: 0 },
          runs: { ...MANDATE.runs, max: null },
        }),
      ),
    ).toBeNull()
  })

  it('names the mandate and takes the high-risk line from what it may spend', () => {
    const small = mandateTouchId(
      mandate({ name: 'DCA ETH', budget: { ...MANDATE.budget, capUsd: 50 } }),
    )
    expect(small.kind).toBe('approve')
    expect(small.reason).toBe(`approve the DCA “DCA ETH”, up to $50.00, on ${MANDATE.chain.name}`)
    expect(mandateTouchId(mandate({ budget: { ...MANDATE.budget, capUsd: 5_000 } })).kind).toBe(
      'approve-high',
    )
  })
})

describe('triggerTouchId', () => {
  it('names the trigger and what it does; high-risk from what one fire moves', () => {
    const ask = triggerTouchId(TRIGGER)
    expect(ask).toEqual({
      kind: 'approve',
      reason: 'arm the trigger “Stop-loss ETH”: sell 50 % of ETH when under $3,800, on Base',
    })
    const big = { ...TRIGGER, action: { ...TRIGGER.action, estimatedUsd: 900 } }
    expect(triggerTouchId(big).kind).toBe('approve-high')
    const unknown = { ...TRIGGER, action: { ...TRIGGER.action, estimatedUsd: null } }
    expect(triggerTouchId(unknown).kind).toBe('approve-high')
    // An alert moves nothing.
    const alert: Trigger = {
      ...TRIGGER,
      kind: 'alert',
      action: { ...TRIGGER.action, kind: 'alert', amountPct: null, estimatedUsd: null },
    }
    expect(triggerTouchId(alert).kind).toBe('approve')
  })
})

describe('triggerAskById', () => {
  it('reads the trigger to name it, and asks as high-risk when it cannot', async () => {
    const call = vi.fn(async () => ({ kind: 'trigger', trigger: TRIGGER }))
    const ask = await triggerAskById(call, 'trg_1a2b3c4d')
    expect(call).toHaveBeenCalledWith('trading.trigger.get', { triggerId: 'trg_1a2b3c4d' })
    expect(ask.reason).toMatch(/^arm the trigger “Stop-loss ETH”/)
    expect((await triggerAskById(call, 'trg_1a2b3c4d', 'fire')).reason).toMatch(
      /^fire the trigger “Stop-loss ETH” now: /,
    )
    const failing = vi.fn(async () => {
      throw new Error('trading.trigger.not_found')
    })
    expect(await triggerAskById(failing, 'trg_9')).toEqual({
      kind: 'approve-high',
      reason: 'arm the trigger trg_9',
    })
    expect((await triggerAskById(failing, 'trg_9', 'fire')).reason).toBe(
      'fire the trigger trg_9 now',
    )
  })
})

const BRACKET = (
  JSON.parse(
    readFileSync('src/renderer/src/views/trading/desk/__fixtures__/bracket/bracket.json', 'utf8'),
  ) as BracketPayload
).bracket

describe('bracketTouchId', () => {
  it('names both lines and the size; high-risk from what the stop would move; an alert moves nothing', () => {
    expect(bracketTouchId(BRACKET)).toEqual({
      kind: 'approve',
      reason:
        'arm the bracket “Protect ETH”: sell 100 % of ETH · take profit over $4,560 · stop under $3,420, on Base',
    })
    expect(bracketTouchId(BRACKET, 'fire').reason).toMatch(/^fire the bracket “Protect ETH” now: /)
    const big = { ...BRACKET, action: { ...BRACKET.action, estimatedUsd: 900 } }
    expect(bracketTouchId(big).kind).toBe('approve-high')
    const unknown = { ...BRACKET, action: { ...BRACKET.action, estimatedUsd: null } }
    expect(bracketTouchId(unknown).kind).toBe('approve-high')
    const alert: Bracket = {
      ...BRACKET,
      kind: 'alert',
      action: { ...BRACKET.action, kind: 'alert', amountPct: null, estimatedUsd: null },
    }
    expect(bracketTouchId(alert).kind).toBe('approve')
  })
})

describe('bracketAskById', () => {
  it('reads the bracket to name it, and asks as high-risk when it cannot', async () => {
    const call = vi.fn(async () => ({ kind: 'bracket', bracket: BRACKET }))
    expect((await bracketAskById(call, 'brk_1a2b3c4d')).reason).toMatch(
      /^arm the bracket “Protect ETH”/,
    )
    expect(call).toHaveBeenCalledWith('trading.bracket.get', { bracketId: 'brk_1a2b3c4d' })
    expect((await bracketAskById(call, 'brk_1a2b3c4d', 'fire')).reason).toMatch(
      /^fire the bracket “Protect ETH” now/,
    )
    const failing = vi.fn(async () => {
      throw new Error('trading.bracket.not_found')
    })
    expect(await bracketAskById(failing, 'brk_9')).toEqual({
      kind: 'approve-high',
      reason: 'arm the bracket brk_9',
    })
  })
})

describe('vaultReason', () => {
  it('names the wallet by label and short address', () => {
    const w = { address: '0x1234567890abcdef1234567890abcdef1234abcd', label: 'Main' }
    expect(vaultReason('export', w)).toBe('export the private key of Main (0x1234…abcd)')
    expect(vaultReason('exportKeystore', w)).toBe('export the keystore of Main (0x1234…abcd)')
    expect(vaultReason('remove', { ...w, label: ' ' })).toBe('remove the wallet 0x1234…abcd')
  })
})

describe('mandateAskById', () => {
  it('reads the mandate to name it', async () => {
    const call = vi.fn(async () => ({ kind: 'mandate', mandate: mandate({ name: 'DCA ETH' }) }))
    expect((await mandateAskById(call, 'dca_1')).reason).toMatch(/^approve the DCA “DCA ETH”/)
    expect(call).toHaveBeenCalledWith('trading.dca.get', { mandateId: 'dca_1' })
    expect((await mandateAskById(call, 'dca_1', 'run')).reason).toMatch(
      /^buy now with the DCA “DCA ETH”: /,
    )
  })

  it('asks as high-risk when the mandate cannot be read', async () => {
    const call = vi.fn(async () => {
      throw new Error('offline')
    })
    expect(await mandateAskById(call, 'dca_1')).toEqual({
      kind: 'approve-high',
      reason: 'approve the DCA dca_1',
    })
    expect((await mandateAskById(call, 'dca_1', 'run')).reason).toBe('buy now with the DCA dca_1')
  })
})

describe('Buy now and a manual trade', () => {
  it('sizes Buy now by one buy, not the whole cap', () => {
    const m = mandate({
      name: 'DCA ETH',
      budget: { ...MANDATE.budget, usdPerRun: 25, capUsd: 5000 },
    })
    expect(mandateTouchId(m).kind).toBe('approve-high')
    expect(mandateTouchId(m, 'run')).toMatchObject({
      kind: 'approve',
      reason: expect.stringMatching(/^buy now with the DCA “DCA ETH”: \$25\.00, on /),
    })
  })

  it('asks a swap or send of unknown worth as high-risk', () => {
    expect(
      manualTradeTouchId('swap', { what: '1 ETH → USDC', chainId: 8453, valueUsd: 20 }),
    ).toEqual({ kind: 'approve', reason: 'swap 1 ETH → USDC on Base' })
    expect(manualTradeTouchId('send', { what: '5 USDC', chainId: 8453, valueUsd: null }).kind).toBe(
      'approve-high',
    )
    expect(
      manualTradeTouchId('bridge', { what: '0.1 ETH → ETH', chainId: 8453, valueUsd: 20 }),
    ).toEqual({ kind: 'approve', reason: 'bridge 0.1 ETH → ETH from Base' })
    expect(manualTradeTouchId('swap', { what: 'x', chainId: 8453, valueUsd: 900 }).kind).toBe(
      'approve-high',
    )
  })
})
