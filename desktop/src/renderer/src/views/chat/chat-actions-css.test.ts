import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const chat = readFileSync('src/renderer/src/views/chat/chat.css', 'utf8')
const desk = readFileSync('src/renderer/src/views/trading/desk/desk.css', 'utf8')

/** The first top-level rule for exactly this selector. */
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return css.match(new RegExp(`(?:^|\\n)${escaped} \\{[\\s\\S]*?\\n\\}`))?.[0] ?? ''
}

/** `[lower, upper]` of the one `@container trd-strip (A <= width < B)` block holding `selector`. */
function step(selector: string): [number, number] {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = chat.match(
    new RegExp(`@container trd-strip \\((\\d+)px <= width < (\\d+)px\\) \\{\\s*${escaped} \\{`),
  )
  expect(m, `no shrink step for ${selector}`).not.toBeNull()
  return [Number(m?.[1]), Number(m?.[2])]
}

// The chat's session actions moved from a header row of their own onto the
// Chat | Trading strip, beside the pill. These guard the geometry that makes
// that row work — it is easy to lose in a later edit and only shows at some
// window sizes.
describe('chat actions on the mode strip', () => {
  it('raises the switch: one row, just tall enough for the pill and its ring', () => {
    const strip = rule(desk, '.trd-strip')
    const row = Number(strip.match(/grid-template-rows: (\d+)px;/)?.[1])
    const pill = Number(rule(desk, '.trd-pill').match(/\n {2}height: (\d+)px;/)?.[1])
    expect(pill).toBeGreaterThan(0)
    // It was 50 px, with a title-and-actions row under it.
    expect(row).toBeLessThan(50)
    // The pill's ring sits 1 px outside it on each side.
    expect(row).toBeGreaterThanOrEqual(pill + 2)
    // A fixed height would clip the second row the narrowest strip needs.
    expect(strip).not.toMatch(/\n {2}height:/)
    expect(chat).not.toMatch(/\.chat-desktop-header__title/)
  })

  it('sheds its labels in order, then gives the actions a row of their own', () => {
    const [stateFrom, stateBelow] = step('.chat-desktop-actions__state-text')
    const [chipFrom, chipBelow] = step('.chat-desktop-actions .proj-chip__name')
    const row = Number(
      chat.match(
        /@container trd-strip \(width < (\d+)px\) \{\s*\.trd-strip\[data-mode='chat'\] > \.trd-strip__right[^{]*\{[^}]*grid-row: 2;/,
      )?.[1],
    )
    expect(row).toBeGreaterThan(0)
    // One label per step, the state's word first: two gone at the same width
    // would cost a whole band information it still had room for.
    expect(stateBelow).toBeGreaterThan(chipBelow)
    expect(chipBelow).toBeGreaterThan(row)
    // The label steps end where the second row begins, so it gets its words back.
    expect(stateFrom).toBe(row)
    expect(chipFrom).toBe(row)
    // The row step must at least leave the three buttons (28 px on a 30 px
    // pitch) and the 12 px off the pill beside a 244 px pill, the width the
    // chain is measured against: two 118 px segments inside 4 px of padding.
    expect(rule(desk, '.trd-pill__seg')).toMatch(/min-width: 118px;/)
    expect(rule(desk, '.trd-pill')).toMatch(/padding: 4px;/)
    expect(row).toBeGreaterThanOrEqual(244 + 2 * (3 * 28 + 2 * 2 + 12))
    expect(rule(desk, '.trd-strip')).toMatch(/container: trd-strip \/ inline-size;/)
  })

  it('only gives the strip that second row when there are actions to put in it', () => {
    // A fresh chat, or the gateway down, leaves the slot empty: moving the
    // empty column down anyway grew the strip by its padding, a blank band
    // under the pill.
    const narrow = chat.match(/@container trd-strip \(width < \d+px\) \{[\s\S]*?\n\}/)?.[0]
    expect(narrow).toMatch(
      /\.trd-strip\[data-mode='chat'\] > \.trd-strip__right:has\(\.chat-desktop-actions\) \{[^}]*grid-row: 2;/,
    )
    expect(narrow?.match(/grid-row: 2;/g)).toHaveLength(1)
  })

  it('folds the run state into its dot without taking the word from screen readers', () => {
    const hidden = chat.match(
      /@container trd-strip \([^)]*\) \{\s*\.chat-desktop-actions__state-text \{[^}]*\}/,
    )?.[0]
    expect(hidden).toMatch(/clip-path: inset\(50%\);/)
    // display: none would drop it from the accessibility tree too.
    expect(hidden).not.toMatch(/display: none/)
  })

  it('never shrinks a glyph, and keeps the actions off the pill however tight', () => {
    expect(chat).toMatch(
      /\.chat-desktop-actions > \*,\s*\.chat-desktop-actions \.proj-chip > :not\(\.proj-chip__name\) \{\s*flex-shrink: 0;/,
    )
    expect(rule(desk, '.trd-strip__session')).toMatch(/padding-left: 12px;/)
  })
})
