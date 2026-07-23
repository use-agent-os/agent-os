import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/approvals/approvals.css', 'utf8')

describe('Approvals decision inbox CSS contract', () => {
  it('uses an integrated decision posture with asymmetric metrics', () => {
    expect(css).toMatch(
      /\.control-surface \.ap-command \.ap-stats \{[\s\S]*?grid-template-columns: 0\.7fr 1\.1fr 1\.25fr;/,
    )
    expect(css).toMatch(/\.ap-command \{[\s\S]*?border-radius: var\(--radius-surface\);/)
    expect(css).toMatch(
      /\.control-surface \.ap-command \.ap-stat:first-child \{[^}]*box-shadow: none;/,
    )
    expect(css).not.toMatch(/\.ap-command \.ap-stat:first-child \{[^}]*inset 2px 0 0/)
  })

  it('keeps the approval inbox primary and policy visible as a secondary control', () => {
    expect(css).toMatch(
      /\.ap-workspace \{[\s\S]*?grid-template-columns: minmax\(0, 1\.45fr\) minmax\(18rem, 0\.7fr\);/,
    )
    expect(css).toMatch(
      /\.control-surface \.ap-strategy\.ap-strategy \{[\s\S]*?position: sticky;[\s\S]*?border-radius: var\(--radius-surface\);/,
    )
    expect(css).toMatch(/\.control-surface \.ap-radio\.is-active \{[^}]*box-shadow: none;/)
  })

  it('stacks safely on narrow screens and respects reduced motion', () => {
    expect(css).toMatch(
      /@media \(max-width: 1020px\)[\s\S]*?\.ap-workspace \{[\s\S]*?grid-template-columns: 1fr;/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.ap-command,[\s\S]*?\.ap-stat__value,[\s\S]*?animation: none;/,
    )
  })
})
