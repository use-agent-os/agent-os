import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/renderer/src/views/trading/trading.css', 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
)

function block(selector: string): string {
  const open = css.indexOf(`\n${selector} {`)
  expect(open).toBeGreaterThanOrEqual(0)
  return css.slice(open, css.indexOf('}', open))
}

// jsdom has no layout, so this is the only place the rule can be held. An
// inline-flex box sizes to its content, so the pool's meta line
// (`Uniswap V4 · dynamic · #1933797`) never ellipsizes and runs over the
// Range column. A block flex box fills the cell and lets the line truncate.
describe('liquidity pool cell CSS', () => {
  it('makes the pool box fill its cell', () => {
    const body = block('.trd-lppool')
    expect(body).toMatch(/display\s*:\s*flex\s*;/)
    expect(body).toMatch(/min-width\s*:\s*0/)
  })
})
