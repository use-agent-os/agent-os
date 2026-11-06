import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function read(path: string): string {
  return readFileSync(path, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
}

const sheets = {
  trading: read('src/renderer/src/views/trading/trading.css'),
  desk: read('src/renderer/src/views/trading/desk/desk.css'),
}

function stackBlocks(css: string): string[] {
  return [...css.matchAll(/([^{}]*)\{([^{}]*)\}/g)]
    .filter(([, selector]) => /\.trd-head__stack\s*(,|$)/.test(selector!.trim()))
    .map(([, , body]) => body!)
}

// jsdom has no layout, so this is the only place the rule can be held. Three
// fanned marks are 54 px wide on the desk and 40 px in the BOOK, wider than
// the mark they stand in for: in a box of the mark's width they spill across
// the gap onto "All wallets".
describe('wallet head stack CSS', () => {
  it.each(Object.entries(sheets))('lets the fan set its own width in %s.css', (_, css) => {
    const blocks = stackBlocks(css)
    expect(blocks.length).toBeGreaterThan(0)
    for (const body of blocks) {
      expect(body).not.toMatch(/(^|[\s;])width\s*:/)
    }
  })
})

function block(css: string, selector: string): string {
  const open = css.indexOf(`\n${selector} {`)
  expect(open).toBeGreaterThanOrEqual(0)
  return css.slice(open, css.indexOf('}', open))
}

// A menu with no height limit runs off the foot of the window once enough
// wallets are imported, and takes Create, Import and Manage with it.
describe('wallet switcher height CSS', () => {
  it('never grows past the window', () => {
    expect(block(sheets.trading, '.mac-menu.trd-head__switcher')).toMatch(
      /max-height:\s*calc\(100vh - 16px\)/,
    )
  })

  it('scrolls the wallet rows and lets them give up height first', () => {
    const list = block(sheets.trading, '.trd-head__wallets')
    expect(list).toMatch(/overflow-y:\s*auto/)
    expect(list).toMatch(/max-height:/)
    expect(list).toMatch(/min-height:\s*0/)
    expect(block(sheets.trading, '.mac-menu.trd-head__switcher > *')).toMatch(/flex-shrink:\s*0/)
    expect(block(sheets.trading, '.mac-menu.trd-head__switcher > .trd-head__wallets')).toMatch(
      /flex-shrink:\s*1/,
    )
  })
})

// The rail's open menu floats outside its row, so nothing there is hovered.
describe('wallet rail open-menu CSS', () => {
  it('keeps the row showing the button while its menu is open', () => {
    expect(sheets.trading).toMatch(/\.trd-wallet__menu:has\(\[aria-expanded='true'\]\) \{/)
    expect(sheets.trading).toMatch(
      /\.trd-wallet:has\(\.trd-wallet__menu \[aria-expanded='true'\]\) \.trd-wallet__value/,
    )
  })
})

describe('wallet head active mark CSS', () => {
  it('keeps the active mark green past the rule that greys the line icons', () => {
    // jsdom has no cascade: `.trd-head__line > svg` outranks a bare class, so
    // the selector has to carry the same weight, and come after it.
    const grey = sheets.trading.indexOf('.trd-head__line > svg {')
    const green = sheets.trading.indexOf('.trd-head__line > .trd-head__active {')
    expect(grey).toBeGreaterThan(-1)
    expect(green).toBeGreaterThan(grey)
    expect(sheets.trading.slice(green, sheets.trading.indexOf('}', green))).toContain(
      'color: var(--ok)',
    )
  })
})
