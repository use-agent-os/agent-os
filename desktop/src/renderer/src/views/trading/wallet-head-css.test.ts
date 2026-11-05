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
