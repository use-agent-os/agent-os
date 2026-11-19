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

// jsdom has no layout, so this is the only place the rules can be held. The
// ticket body scrolls down, which makes its other axis `auto` too: one figure
// wider than its leg (a nine-digit receive amount) gave the whole ticket a
// sideways scrollbar.
describe('ticket amount CSS', () => {
  it('never scrolls the ticket sideways', () => {
    expect(block('.trd-ticket__body')).toMatch(/overflow-x\s*:\s*hidden\s*;/)
  })

  it('clips a read-only amount inside its leg', () => {
    const body = block('output.trd-amount')
    expect(body).toMatch(/overflow\s*:\s*hidden\s*;/)
    expect(body).toMatch(/white-space\s*:\s*nowrap\s*;/)
    expect(body).toMatch(/text-overflow\s*:\s*ellipsis\s*;/)
  })
})
