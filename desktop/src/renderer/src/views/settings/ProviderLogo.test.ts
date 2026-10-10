import { describe, expect, it } from 'vitest'
import { hasProviderMark } from './ProviderLogo'

describe('hasProviderMark', () => {
  it('bundles a brand mark for every first-party gateway', () => {
    for (const id of ['opencap', 'bankr', 'surplus', 'openrouter']) {
      expect(hasProviderMark(id), id).toBe(true)
    }
  })

  it('leaves unknown ids to the monogram', () => {
    expect(hasProviderMark('acme-llm')).toBe(false)
  })
})
