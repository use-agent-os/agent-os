import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/renderer/src/components/menu/menu.css', 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
)

// Menus answer the pointer like NSMenu's. PopMenu.tsx opens a submenu the
// moment the pointer reaches its row; these keep the stylesheet from
// putting the lag back in between.
describe('menu CSS timing contract', () => {
  it('eases nothing, so the row highlight lands under the pointer at once', () => {
    expect(css).not.toMatch(/transition/)
  })

  it('shows a submenu panel without an entry animation', () => {
    const panel = css.match(/\.mac-menu__sub > \.mac-menu \{[^}]*\}/)?.[0]
    expect(panel).toMatch(/animation: none;/)
    expect(css).not.toMatch(/@keyframes mac-submenu-in/)
  })

  it('keeps the menu entry brief, and drops it under reduced motion', () => {
    const ms = Number(css.match(/\.mac-menu \{[^}]*animation: mac-menu-in (\d+)ms/)?.[1])
    expect(ms).toBeGreaterThan(0)
    expect(ms).toBeLessThanOrEqual(80)
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{\s*\.mac-menu \{\s*animation: none;\s*\}/,
    )
  })
})
