// docs/web-ui.md lists exactly the web console's binding table (task 5.6).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { COMPOSER_BINDINGS, GLOBAL_BINDINGS, NAVIGATION_CHORDS } from './bindings'

const doc = readFileSync('../docs/web-ui.md', 'utf8')
const section = doc.slice(doc.indexOf('## Keyboard Shortcuts'), doc.indexOf('## Agent Setup'))

function keysUnder(heading: string): string[] {
  const start = section.indexOf(heading)
  const rest = section.slice(start + heading.length)
  const end = rest.search(/\n### |\n> /)
  return [...(end >= 0 ? rest.slice(0, end) : rest).matchAll(/^\| `([^`]+)` \|/gm)].map((m) =>
    m[1]!.toLowerCase(),
  )
}

describe('docs/web-ui.md keyboard shortcuts', () => {
  it('documents exactly the global bindings', () => {
    expect(keysUnder('### Global')).toEqual(GLOBAL_BINDINGS.map((b) => b.combo))
  })

  it('documents exactly the navigation chords', () => {
    expect(keysUnder('### Navigation chords')).toEqual(NAVIGATION_CHORDS.map((b) => b.combo))
  })

  it('documents exactly the composer keys', () => {
    expect(keysUnder('### Composer')).toEqual(COMPOSER_BINDINGS.map((b) => b.combo))
  })
})
