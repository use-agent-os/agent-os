import { keccak_256 } from '@noble/hashes/sha3.js'

/**
 * The terminal's pure helpers live in the console's shared layer
 * (`frontend/src/views/trading/logic.ts`), which the web console's read-only
 * desk uses too. Only the EIP-55 checksum stays here: it needs keccak, a
 * desktop dependency, and only the desktop's send forms check a typed address.
 */
export * from '@/views/trading/logic'

// ── Addresses ───────────────────────────────────────────────────────────────

const HEX_ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/

/**
 * EIP-55: the address with its letters cased by the keccak of its lowercase
 * hex. Null when the input is not 20 bytes of hex at all.
 */
export function checksumAddress(address: string): string | null {
  const trimmed = address.trim()
  if (!HEX_ADDRESS_RE.test(trimmed)) return null
  const lower = trimmed.slice(2).toLowerCase()
  const hash = keccak_256(new TextEncoder().encode(lower))
  let out = '0x'
  for (let i = 0; i < lower.length; i++) {
    const ch = lower[i] as string
    // One hex digit of the hash per character: the high nibble for even
    // positions, the low one for odd.
    const nibble = i % 2 === 0 ? (hash[i >> 1] as number) >> 4 : (hash[i >> 1] as number) & 0x0f
    out += /[a-f]/.test(ch) && nibble >= 8 ? ch.toUpperCase() : ch
  }
  return out
}

/**
 * A mixed-case address whose casing is not its checksum: almost always a
 * character mangled in copying, which a lowercase-only compare would let
 * through. All-lower and all-upper carry no checksum and pass.
 */
export function checksumMismatch(address: string): boolean {
  const trimmed = address.trim()
  if (!HEX_ADDRESS_RE.test(trimmed)) return false
  const body = trimmed.slice(2)
  if (body === body.toLowerCase() || body === body.toUpperCase()) return false
  return checksumAddress(trimmed) !== trimmed
}
