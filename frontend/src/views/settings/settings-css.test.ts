import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/views/settings/settings.css', 'utf8')

describe('Settings responsive CSS contract', () => {
  it('uses a standard Control command surface instead of translucent route chrome', () => {
    expect(css).toMatch(
      /\.settings-toolbar \{[\s\S]*?border: 1px solid var\(--border\);[\s\S]*?background: var\(--surface\);/,
    )
    expect(css).toMatch(
      /\.settings-surface-tabs button\.is-active \{[\s\S]*?border-color: color-mix\([\s\S]*?background: color-mix\(/,
    )
    expect(css).not.toContain('.settings-header__mark')
  })

  it('stacks the workspace controls before mobile content', () => {
    expect(css).toMatch(
      /@media \(max-width: 960px\)[\s\S]*?\.settings-toolbar \{[\s\S]*?flex-direction: column;/,
    )
    expect(css).toMatch(
      /@media \(max-width: 620px\)[\s\S]*?\.settings-surface-tabs \{\s*width: 100%;/,
    )
  })

  it('uses a two-column mobile glance instead of a telemetry strip', () => {
    expect(css).toMatch(
      /@media \(max-width: 620px\)[\s\S]*?\.settings-glance \{[\s\S]*?grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/,
    )
    expect(css).not.toContain('.settings-context')
  })
})
