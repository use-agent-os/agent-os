import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/channels/channels.css', 'utf8')

describe('Channels integration workspace CSS contract', () => {
  it('integrates channel metrics into one posture surface', () => {
    expect(css).toMatch(
      /\.control-surface \.ch-command \.ch-stats \{[\s\S]*?grid-template-columns: 1\.25fr repeat\(4, minmax\(0, 1fr\)\);/,
    )
    expect(css).toMatch(
      /\.control-surface \.ch-command \.ch-stat \{[\s\S]*?border-left: 1px solid var\(--hairline\);[\s\S]*?border-radius: 0;/,
    )
    expect(css).toMatch(
      /\.control-surface \.ch-command \.ch-stat:first-child \{[^}]*box-shadow: none;/,
    )
    expect(css).not.toMatch(/\.ch-command \.ch-stat:first-child \{[^}]*inset 2px 0 0/)
  })

  it('presents adapters as rounded status-aware inventory surfaces', () => {
    expect(css).toMatch(
      /\.control-surface \.ch-card\.ch-card \{[\s\S]*?border-radius: var\(--radius-surface\);/,
    )
    expect(css).toMatch(
      /\.control-surface \.ch-card\.ch-card::before \{[\s\S]*?background: var\(--tone, var\(--dim\)\);/,
    )
    expect(css).toMatch(
      /\.ch-card__mark \{[\s\S]*?width: 2\.2rem;[\s\S]*?height: 2\.2rem;[\s\S]*?color: var\(--primary\);/,
    )
    expect(css).toMatch(/\.ch-card__mark > svg \{[\s\S]*?width: 1\.25rem;/)
    expect(css).toMatch(/\.ch-access \{[\s\S]*?border-top: 1px solid var\(--hairline\);/)
  })

  it('uses a viewport-anchored, rounded setup surface with compact responsive fields', () => {
    expect(css).toMatch(/\.ch-setup-overlay \{[\s\S]*?position: fixed;[\s\S]*?z-index: 120;/)
    expect(css).toMatch(
      /\.ch-setup-dialog \{[\s\S]*?width: min\(58rem, 100%\);[\s\S]*?border-radius: calc\(var\(--radius-surface\) \+ 0\.25rem\);/,
    )
    expect(css).toMatch(
      /@media \(max-width: 768px\)[\s\S]*?\.ch-setup__types,[\s\S]*?\.ch-setup__fields \{[\s\S]*?grid-template-columns: 1fr;/,
    )
    expect(css).toMatch(/\.ch-setup__type-mark > svg \{[\s\S]*?width: 1\.15rem;/)
  })

  it('keeps setup controls aligned and gives the enabled control its own row', () => {
    expect(css).toMatch(/\.ch-setup__field \{[\s\S]*?height: 100%;[\s\S]*?flex-direction: column;/)
    expect(css).toMatch(/\.ch-setup__field\.is-wide \{[\s\S]*?grid-column: 1 \/ -1;/)
    expect(css).toMatch(/\.ch-setup__input,[\s\S]*?\.ch-setup__select \{[\s\S]*?margin-top: auto;/)
    expect(css).toMatch(
      /\.ch-setup__check \{[\s\S]*?min-height: 2\.65rem;[\s\S]*?margin-top: auto;/,
    )
  })

  it('collapses cleanly and respects reduced motion', () => {
    expect(css).toMatch(
      /@media \(max-width: 520px\)[\s\S]*?\.control-surface \.ch-command \.ch-stats,[\s\S]*?\.ch-card__meta \{[\s\S]*?grid-template-columns: 1fr;/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.ch-command,[\s\S]*?\.ch-stat__value,[\s\S]*?\.ch-command__cadence > span,[\s\S]*?\.ch-refresh-spin,[\s\S]*?\.ch-setup__types button \{[\s\S]*?animation: none;/,
    )
  })
})
