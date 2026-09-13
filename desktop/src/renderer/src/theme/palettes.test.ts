import { describe, expect, it } from 'vitest'
import { PALETTE_IDS } from '@shared/theme'
import { COLOR_TOKEN_KEYS, PALETTES } from './palettes'

describe('palettes', () => {
  it('registers every PaletteId from the shared contract', () => {
    for (const id of PALETTE_IDS) expect(PALETTES[id]?.id).toBe(id)
  })

  it('defines every token for both modes in every palette', () => {
    for (const id of PALETTE_IDS) {
      for (const mode of ['light', 'dark'] as const) {
        const tokens = PALETTES[id][mode]
        for (const key of COLOR_TOKEN_KEYS) {
          expect(tokens[key], `${id}.${mode}.${key}`).toMatch(/\S/)
        }
      }
    }
  })

  it('uses distinct primary colours per mode so signal survives a flip', () => {
    for (const id of PALETTE_IDS) {
      expect(PALETTES[id].light.primary).not.toBe(PALETTES[id].dark.primary)
    }
  })
})
