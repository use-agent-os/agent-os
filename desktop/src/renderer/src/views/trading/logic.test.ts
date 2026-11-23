import { describe, expect, it } from 'vitest'
import { checksumAddress, checksumMismatch } from './logic'

describe('checksumAddress (EIP-55)', () => {
  // The EIP's own test vectors.
  const VECTORS = [
    '0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed',
    '0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359',
    '0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB',
    '0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb',
  ]

  it('reproduces the reference casing from any casing', () => {
    for (const v of VECTORS) {
      expect(checksumAddress(v.toLowerCase())).toBe(v)
      expect(checksumAddress(v.toUpperCase().replace('0X', '0x'))).toBe(v)
      expect(checksumAddress(v)).toBe(v)
    }
  })

  it('refuses what is not an address', () => {
    expect(checksumAddress('vitalik.eth')).toBeNull()
    expect(checksumAddress('0x1234')).toBeNull()
  })

  it('flags a mixed-case address whose casing is wrong, and only that', () => {
    const v = VECTORS[0]!
    // One letter flipped: a mangled paste.
    const bad =
      v.slice(0, 5) +
      (v[5] === v[5]!.toUpperCase() ? v[5]!.toLowerCase() : v[5]!.toUpperCase()) +
      v.slice(6)
    expect(checksumMismatch(bad)).toBe(true)
    expect(checksumMismatch(v)).toBe(false)
    // No checksum to check: all-lower and all-upper pass through.
    expect(checksumMismatch(v.toLowerCase())).toBe(false)
    expect(checksumMismatch('0x' + v.slice(2).toUpperCase())).toBe(false)
    expect(checksumMismatch('not-an-address')).toBe(false)
  })
})
