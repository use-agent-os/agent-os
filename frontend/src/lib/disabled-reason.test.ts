import { describe, expect, it } from 'vitest'
import {
  blockedBecause,
  gate,
  OPEN_GATE,
  requireConnection,
  requireNoPendingApprovals,
  requireScheduler,
} from './disabled-reason'

describe('gate', () => {
  it('is open when every check passes', () => {
    expect(gate(requireConnection('connected'), requireScheduler(false), null, false)).toEqual(
      OPEN_GATE,
    )
  })

  it('takes the first block, with its code and words', () => {
    expect(gate(requireConnection('disconnected'), requireScheduler(true))).toEqual({
      disabled: true,
      code: 'gateway_offline',
      reason: 'The gateway is offline. Reconnect to use this.',
    })
    expect(gate(requireConnection('connected'), requireScheduler(true)).code).toBe(
      'scheduler_unavailable',
    )
    expect(gate(requireConnection('connecting')).reason).toBe('Waiting for the gateway connection.')
  })

  it('pluralises the approvals reason and accepts a custom one', () => {
    expect(requireNoPendingApprovals(0)).toBeNull()
    expect(requireNoPendingApprovals(1)?.reason).toBe(
      '1 approval is waiting. Answer it before updating.',
    )
    expect(requireNoPendingApprovals(3)?.reason).toBe(
      '3 approvals are waiting. Answer them before updating.',
    )
    expect(gate(blockedBecause('Not here'))).toMatchObject({ code: 'custom', reason: 'Not here' })
  })
})
