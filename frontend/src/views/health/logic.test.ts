import { describe, expect, it } from 'vitest'
import {
  impactValue,
  impactCountsFromSeverity,
  statusLabel,
  findingGroupKind,
  shellArg,
  isLocalGatewayUrl,
  gatewayStatusTarget,
  visibleEvidenceEntries,
  evidenceLabel,
  evidenceValue,
} from './logic'

describe('impactValue', () => {
  it('passes through valid readinessImpact', () => {
    expect(impactValue({ readinessImpact: 'degrades' })).toBe('degrades')
  })
  it.each([
    ['error', 'blocks_ready'],
    ['warn', 'degrades'],
    ['info', 'optional'],
    ['ok', 'none'],
  ])('maps severity %s -> %s', (severity, impact) => {
    expect(impactValue({ severity })).toBe(impact)
  })
})

describe('impactCountsFromSeverity', () => {
  it('maps severity counts to impact counts', () => {
    expect(impactCountsFromSeverity({ error: 2, warn: 1, info: 3, ok: 4 })).toEqual({
      blocks_ready: 2,
      degrades: 1,
      optional: 3,
      none: 4,
    })
  })
})

describe('statusLabel', () => {
  it('shows "Ready with warnings" when ready but degraded', () => {
    expect(statusLabel('degraded', true)).toBe('Ready with warnings')
  })
  it('maps action_required', () => {
    expect(statusLabel('action_required', false)).toBe('Action required')
  })
})

describe('findingGroupKind', () => {
  it('maps blocks_ready to action', () => {
    expect(findingGroupKind({ readinessImpact: 'blocks_ready' })).toBe('action')
  })
})

describe('shellArg', () => {
  it('passes safe strings through', () => {
    expect(shellArg('/tmp/agentos.toml')).toBe('/tmp/agentos.toml')
  })
  it('quotes and escapes unsafe strings', () => {
    expect(shellArg("it's here")).toBe("'it'\\''s here'")
  })
})

describe('gateway url helpers', () => {
  it('treats loopback hosts as local', () => {
    expect(isLocalGatewayUrl('ws://127.0.0.1:18791/ws')).toBe(true)
    expect(isLocalGatewayUrl('wss://prod.example.com/ws')).toBe(false)
  })
  it('normalizes 0.0.0.0 and infers default port', () => {
    expect(gatewayStatusTarget('ws://0.0.0.0/ws')).toEqual({ host: '127.0.0.1', port: '18791' })
    expect(gatewayStatusTarget('wss://h.example/ws')).toEqual({ host: 'h.example', port: '443' })
  })
})

describe('evidence', () => {
  it('hides restart keys and null values', () => {
    const entries = visibleEvidenceEntries({ a: 1, restartRequired: true, b: null })
    expect(entries).toEqual([['a', 1]])
  })
  it('labels camelCase keys', () => {
    expect(evidenceLabel('gatewayUrl')).toBe('Gateway url')
  })
  it('truncates long JSON values at 120 chars', () => {
    const long = { k: 'x'.repeat(200) }
    expect(evidenceValue(long).length).toBe(120)
    expect(evidenceValue(long).endsWith('...')).toBe(true)
  })
})
