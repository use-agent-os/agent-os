import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/logs/logs.css', 'utf8')

describe('Logs observability console CSS contract', () => {
  it('integrates status, metrics, filters and stream into one console surface', () => {
    expect(css).toMatch(/\.lg-console \{[\s\S]*?border-radius: var\(--radius-surface\);/)
    expect(css).toMatch(
      /\.control-surface \.lg-console \.lg-stats \{[\s\S]*?grid-template-columns: 1\.25fr repeat\(3, minmax\(0, 1fr\)\);/,
    )
    expect(css).toMatch(
      /\.control-surface \.lg-console \.lg-stat:first-child \{[^}]*box-shadow: none;/,
    )
    expect(css).not.toMatch(/\.lg-console \.lg-stat:first-child \{[^}]*inset 2px 0 0/)
    expect(css).toMatch(/\.lg-console \.lg-stream \{[\s\S]*?border: 0;[\s\S]*?border-radius: 0;/)
  })

  it('keeps the dense live stream bounded to the viewport', () => {
    expect(css).toMatch(
      /\.lg-console \.lg-display \{[\s\S]*?height: clamp\(28rem, calc\(100dvh - 29rem\), 44rem\);/,
    )
    expect(css).toMatch(
      /\.lg-console \.lg-line \{[\s\S]*?grid-template-columns: 10\.5rem 4rem minmax\(0, 1fr\);/,
    )
  })

  it('reflows controls and removes motion when requested', () => {
    expect(css).toMatch(
      /@media \(max-width: 768px\)[\s\S]*?\.lg-console \.lg-toolbar \{[\s\S]*?flex-direction: column;/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.lg-console,[\s\S]*?\.lg-stat__value,[\s\S]*?\.lg-line \{[\s\S]*?animation: none;/,
    )
  })
})
