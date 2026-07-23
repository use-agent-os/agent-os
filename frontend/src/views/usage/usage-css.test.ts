import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/usage/usage.css', 'utf8')

describe('Usage analytics CSS contract', () => {
  it('uses one integrated summary surface instead of four equal KPI cards', () => {
    expect(css).toMatch(
      /\.usage-overview__body \{[\s\S]*?grid-template-columns: minmax\(15rem, 1\.05fr\)[\s\S]*?minmax\(13rem, 0\.75fr\);/,
    )
    expect(css).toMatch(/\.usage-overview::before \{[\s\S]*?background: linear-gradient/)
    expect(css).not.toMatch(/\.usage-stats|\.usage-stat(?:__|\s|\.)/)
  })

  it('renders model allocation as a comparison ledger with direct numeric columns', () => {
    expect(css).toMatch(
      /\.usage-model-card \{[\s\S]*?display: grid;[\s\S]*?grid-template-columns: 2rem minmax\(12rem, 1\.25fr\)/,
    )
    expect(css).toMatch(
      /\.usage-model-card__rows \{[\s\S]*?grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/,
    )
  })

  it('prevents page-level overflow and adapts analytics controls for small screens', () => {
    expect(css).toMatch(
      /@media \(max-width: 760px\)[\s\S]*?\.usage-range \{[\s\S]*?grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/,
    )
    expect(css).toMatch(/\.usage-table-wrap \{[\s\S]*?overflow: auto;/)
  })

  it('limits feedback motion and provides a reduced-motion fallback', () => {
    expect(css).toMatch(/\.usage-bar-row \{[\s\S]*?animation: usage-reveal 260ms both;/)
    expect(css).toMatch(
      /\.usage-bar-row__fill \{[\s\S]*?animation: usage-bar-grow 420ms var\(--usage-motion-ease\) both;/,
    )
    expect(css).toMatch(
      /\.usage-model-card__share-fill \{[\s\S]*?animation: usage-bar-grow 440ms var\(--usage-motion-ease\) both;/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.usage-model-card__share-fill,[\s\S]*?\.usage-expand__share-fill \{[\s\S]*?animation: none;/,
    )
    expect(css).toMatch(
      /\.usage-bar-row__fill,[\s\S]*?\.usage-expand__share-fill \{[\s\S]*?transform: none;[\s\S]*?transition: none;/,
    )
  })
})
