import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/health/health.css', 'utf8')

describe('Health readiness CSS contract', () => {
  it('uses one integrated readiness surface with a compact impact profile', () => {
    expect(css).toMatch(
      /\.control-surface \.health-status__rail\.health-status__rail \{[\s\S]*?grid-template-columns: minmax\(17rem, 0\.88fr\)[\s\S]*?minmax\(28rem, 1\.45fr\);/,
    )
    expect(css).toMatch(/\.health-impact-meter \{[\s\S]*?border-radius: var\(--radius-pill\);/)
    expect(css).toMatch(
      /\.health-count \{[\s\S]*?grid-template-columns: auto minmax\(0, 1fr\) auto;/,
    )
  })

  it('keeps diagnostics in a readable severity-ordered triage feed', () => {
    expect(css).toMatch(
      /\.control-surface \.health-finding-group\.health-finding-group \{[\s\S]*?border-radius: var\(--radius-surface\);/,
    )
    expect(css).toMatch(
      /\.health-finding-group__header \{[\s\S]*?box-shadow: inset 2px 0 0 color-mix/,
    )
    expect(css).toMatch(/\.health-finding \{[\s\S]*?grid-template-columns: 1rem minmax\(0, 1fr\);/)
    expect(css).toMatch(
      /\.control-surface \.health-finding-group\.health-finding-group\.is-action \{[\s\S]*?--tone: var\(--danger\);/,
    )
  })

  it('adapts the readiness surface and context for narrow screens', () => {
    expect(css).toMatch(
      /@media \(max-width: 980px\)[\s\S]*?\.control-surface \.health-status__rail\.health-status__rail \{[\s\S]*?grid-template-columns: 1fr;/,
    )
    expect(css).toMatch(
      /@media \(max-width: 700px\)[\s\S]*?\.health-count-grid \{[\s\S]*?grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/,
    )
  })

  it('uses short feedback motion with a reduced-motion fallback', () => {
    expect(css).toMatch(
      /\.health-impact-meter__segment \{[\s\S]*?animation: health-meter-grow 440ms var\(--health-motion-ease\) forwards;/,
    )
    expect(css).toMatch(
      /\.control-surface \.health-finding-group\.health-finding-group \{[\s\S]*?animation: health-group-enter 320ms var\(--health-motion-ease\) both;/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.health-impact-meter__segment,[\s\S]*?\.health-finding-group \{[\s\S]*?animation: none;/,
    )
  })
})
