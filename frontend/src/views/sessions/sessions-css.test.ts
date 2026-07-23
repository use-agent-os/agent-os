import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/sessions/sessions.css', 'utf8')

describe('Sessions activity ledger CSS contract', () => {
  it('uses one asymmetric session pulse instead of detached KPI cards', () => {
    expect(css).toMatch(
      /\.control-surface \.sess-command \.sess-stats \{[\s\S]*?grid-template-columns: 1\.45fr repeat\(2, minmax\(0, 1fr\)\);/,
    )
    expect(css).toMatch(
      /\.control-surface \.sess-command \.sess-stat \{[\s\S]*?border-left: 1px solid var\(--hairline\);[\s\S]*?border-radius: 0;/,
    )
    expect(css).toMatch(
      /\.control-surface \.sess-command \.sess-stat:first-child \{[^}]*box-shadow: none;/,
    )
    expect(css).not.toMatch(/\.sess-command \.sess-stat:first-child \{[^}]*inset 2px 0 0/)
  })

  it('keeps search, selection, and the table in one rounded ledger', () => {
    expect(css).toMatch(/\.sess-list \{[\s\S]*?border-radius: var\(--radius-surface\);/)
    expect(css).toMatch(
      /\.sess-bulk-bar \{[\s\S]*?border-bottom: 1px solid color-mix[\s\S]*?border-radius: 0;/,
    )
    expect(css).toMatch(
      /\.control-surface \.sess-list \.sess-table-wrap \{[\s\S]*?overflow: auto;[\s\S]*?border: 0;/,
    )
  })

  it('provides responsive containment and reduced-motion fallbacks', () => {
    expect(css).toMatch(
      /@media \(max-width: 768px\)[\s\S]*?\.control-surface \.sess-command \.sess-stats \{[\s\S]*?grid-template-columns: 1fr;/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.sess-command,[\s\S]*?\.sess-list,[\s\S]*?\.sess-stat__value,[\s\S]*?animation: none;/,
    )
  })
})
