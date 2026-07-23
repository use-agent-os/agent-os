import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/skills/skills.css', 'utf8')
const controlCss = readFileSync('src/styles/control-surface.css', 'utf8')

describe('Skills directory CSS contract', () => {
  it('uses a four-source directory navigator with a two-column mobile fallback', () => {
    expect(css).toMatch(
      /\.control-surface \.sk-tabs \{[\s\S]*?grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/,
    )
    expect(css).toMatch(
      /@media \(max-width: 760px\)[\s\S]*?\.control-surface \.sk-tabs \{[\s\S]*?grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/,
    )
  })

  it('keeps installed status filters compact instead of rendering KPI cards', () => {
    expect(css).toMatch(/\.control-surface \.sk-metrics \{[\s\S]*?display: flex;[\s\S]*?margin: 0;/)
    expect(css).toMatch(
      /\.control-surface \.sk-metric \{[\s\S]*?min-height: 3rem;[\s\S]*?box-shadow: none;/,
    )
    expect(controlCss).not.toMatch(/\.sk-metrics|\.sk-metric(?:__|\s|\.)/)
  })

  it('shows brand artwork at full color and uses readable text statuses', () => {
    expect(css).toMatch(/\.control-surface \.sk-tab__brand \{[\s\S]*?object-fit: cover;/)
    expect(css).toMatch(
      /\.control-surface \.sk-rcard__logo,[\s\S]*?filter: none;[\s\S]*?opacity: 1;/,
    )
    expect(css).toMatch(/\.control-surface \.sk-card__status \{[\s\S]*?font-size:/)
  })

  it('keeps feedback motion subtle and disables transforms for reduced motion', () => {
    expect(css).toMatch(/\.control-surface \.sk-card:hover \{[\s\S]*?translateY\(-1px\)/)
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.control-surface \.sk-card:hover,[\s\S]*?transform: none;/,
    )
  })
})
