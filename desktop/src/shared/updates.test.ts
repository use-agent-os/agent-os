import { describe, expect, it } from 'vitest'
import { compareVersions, gatewaySupported, isNewer, MIN_GATEWAY_VERSION } from './updates'

describe('compareVersions', () => {
  it('orders CalVer numerically, not lexically', () => {
    expect(isNewer('2026.9.11', '2026.9.9')).toBe(true)
    expect(isNewer('2026.10.1', '2026.9.30')).toBe(true)
    expect(isNewer('2027.1.1', '2026.12.31')).toBe(true)
    expect(isNewer('2026.9.9', '2026.9.9')).toBe(false)
  })

  it('treats .postN as after the release but before the next one', () => {
    expect(isNewer('2026.9.9.post1', '2026.9.9')).toBe(true)
    expect(isNewer('2026.9.10', '2026.9.9.post1')).toBe(true)
    expect(isNewer('2026.9.9.post2', '2026.9.9.post1')).toBe(true)
  })

  it('sorts pre-releases and local builds below the plain release', () => {
    expect(compareVersions('2026.9.9rc1', '2026.9.9')).toBeLessThan(0)
    expect(compareVersions('0.0.0+unknown', '0.0.0')).toBeLessThan(0)
    expect(compareVersions('v2026.9.9', '2026.9.9')).toBe(0)
  })
})

describe('gatewaySupported', () => {
  it('accepts the minimum and anything newer, including build suffixes', () => {
    expect(gatewaySupported(MIN_GATEWAY_VERSION)).toBe(true)
    expect(gatewaySupported(`${MIN_GATEWAY_VERSION}+abc123`)).toBe(true)
    expect(gatewaySupported('2999.1.1')).toBe(true)
  })

  it('rejects an older gateway and tolerates an unknown one', () => {
    expect(gatewaySupported('2026.8.23')).toBe(false)
    expect(gatewaySupported(null)).toBe(true)
    expect(gatewaySupported(undefined)).toBe(true)
  })
})
