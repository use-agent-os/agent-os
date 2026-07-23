import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/overview/overview.css', 'utf8')

describe('Overview command center CSS contract', () => {
  it('uses one asymmetric system-pulse surface instead of five equal KPI cards', () => {
    expect(css).toMatch(
      /\.ov-command__body \{[\s\S]*?grid-template-columns: minmax\(17rem, 0\.88fr\)[\s\S]*?minmax\(30rem, 1\.55fr\);/,
    )
    expect(css).toMatch(
      /\.ov-command__metrics \{[\s\S]*?grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/,
    )
    expect(css).toMatch(
      /\.control-surface \.ov-command \.ov-stat--hero \{[\s\S]*?box-shadow: inset 2px 0 0 color-mix/,
    )
  })

  it('orders sessions and live activity before the connection utility', () => {
    expect(css).toMatch(
      /\.ov-grid \{[\s\S]*?grid-template-areas:[\s\S]*?'recent events'[\s\S]*?'connection connection';/,
    )
    expect(css).toMatch(/\.ov-panel--recent \{[\s\S]*?grid-area: recent;/)
    expect(css).toMatch(/\.ov-panel--events \{[\s\S]*?grid-area: events;/)
    expect(css).toMatch(/\.ov-panel--conn \{[\s\S]*?grid-area: connection;/)
  })

  it('keeps the operational surfaces usable at narrow widths', () => {
    expect(css).toMatch(
      /@media \(max-width: 1100px\)[\s\S]*?grid-template-areas:[\s\S]*?'recent'[\s\S]*?'events'[\s\S]*?'connection';/,
    )
    expect(css).toMatch(
      /@media \(max-width: 700px\)[\s\S]*?\.ov-form \{[\s\S]*?grid-template-columns: 1fr;/,
    )
    expect(css).toMatch(
      /@media \(max-width: 480px\)[\s\S]*?\.ov-command__metrics \{[\s\S]*?repeat\(2, minmax\(0, 1fr\)\);/,
    )
  })

  it('uses quick motion feedback and disables it for reduced motion', () => {
    expect(css).toMatch(
      /\.ov-recent__row \{[\s\S]*?animation: ov-row-enter 260ms var\(--ov-motion-ease\) both;/,
    )
    expect(css).toMatch(
      /\.ov-event-log__row\.is-fresh \{[\s\S]*?animation: ov-event-fresh 620ms var\(--ov-motion-ease\) both;/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.ov-event-log__row\.is-fresh \{[\s\S]*?animation: none;/,
    )
  })
})
