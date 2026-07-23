import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/setup/setup.css', 'utf8')

describe('Setup embedded surface CSS contract', () => {
  it('keeps the inner panel out of the rounded clipping context', () => {
    expect(css).toMatch(
      /\.setup-stage--embedded \.setup-panel,[\s\S]*?\.control-surface \.setup-stage--embedded \.setup-panel \{[\s\S]*?overflow: visible;/,
    )
  })

  it('gives router selects and capability checkboxes dedicated control styling', () => {
    expect(css).toMatch(
      /\.setup-select select \{[\s\S]*?appearance: none;[\s\S]*?padding: 0\.58rem 2\.35rem 0\.58rem 0\.75rem;/,
    )
    expect(css).toMatch(
      /\.setup-router-toolbar input\[type='number'\] \{[\s\S]*?box-sizing: border-box;[\s\S]*?padding: 0\.58rem 2\.65rem 0\.58rem 0\.75rem;/,
    )
    expect(css).toMatch(
      /\.setup-check__input:checked \+ \.setup-check__control \{[\s\S]*?background: var\(--primary\);/,
    )
    expect(css).toMatch(/\.setup-check > \.setup-check__control \{[\s\S]*?color: transparent;/)
    expect(css).toMatch(
      /\.setup-capability-toggle \{[\s\S]*?width: 100%;[\s\S]*?border-radius: var\(--radius-control\);/,
    )
    expect(css).toContain(".setup-tier-table input:not([type='checkbox'])")
  })

  it('uses a soft disclosure surface instead of another hard nested border', () => {
    expect(css).toMatch(/\.setup-advanced \{[\s\S]*?border: 0;[\s\S]*?background: color-mix\(/)
  })

  it('reflows router tier rows into labelled mobile fields', () => {
    expect(css).toMatch(
      /@media \(max-width: 720px\) \{[\s\S]*?\.setup-tier-table__row \{[\s\S]*?grid-template-columns: minmax\(0, 1fr\) 6\.5rem;/,
    )
  })
})
