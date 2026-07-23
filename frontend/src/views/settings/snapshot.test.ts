import { describe, expect, it } from 'vitest'
import { isMethodNotFoundRpcError, readinessFromSnapshot } from './snapshot'

describe('readinessFromSnapshot', () => {
  it('counts degraded and explicitly action-required sections as needing attention', () => {
    expect(
      readinessFromSnapshot({
        readiness: {
          sectionDetails: {
            llm: { status: 'ok', required: true },
            router: { status: 'degraded', required: true },
            search: { status: 'unknown' },
          },
        },
      }),
    ).toEqual({ total: 3, ready: 1, actionRequired: 2, required: 1, optional: 1 })
  })

  it('does not report a ready agent when runtime readiness fails without section details', () => {
    expect(readinessFromSnapshot({ readiness: { runtimeReady: false } })).toEqual({
      total: 1,
      ready: 0,
      actionRequired: 1,
      required: 1,
      optional: 0,
    })
  })

  it('honors runtime action state even when precomputed counts are stale', () => {
    expect(
      readinessFromSnapshot({
        readiness: {
          state: 'action_required',
          total: 4,
          ready: 4,
          actionRequired: 0,
          required: 0,
          optional: 0,
        },
      }),
    ).toMatchObject({ ready: 3, actionRequired: 1 })
  })
})

describe('isMethodNotFoundRpcError', () => {
  it('matches only the explicit RPC compatibility code', () => {
    expect(isMethodNotFoundRpcError({ code: 'METHOD_NOT_FOUND' })).toBe(true)
    expect(isMethodNotFoundRpcError({ code: 'FORBIDDEN' })).toBe(false)
    expect(isMethodNotFoundRpcError(new Error('METHOD_NOT_FOUND'))).toBe(false)
  })
})
