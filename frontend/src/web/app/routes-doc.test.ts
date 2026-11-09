// docs/web-ui.md documents exactly the route table and the legacy redirects
// (overhaul-control-ui task 3.14).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { legacyRedirect } from './legacy-redirect'
import { DESTINATION_ROUTES, LEGACY_PATHS } from './routes'

const doc = readFileSync('../docs/web-ui.md', 'utf8')

function tableAfter(heading: string): string[][] {
  const start = doc.indexOf(heading)
  expect(start, heading).toBeGreaterThanOrEqual(0)
  const rows: string[][] = []
  let inTable = false
  for (const line of doc.slice(start).split('\n').slice(1)) {
    if (line.startsWith('|')) {
      inTable = true
      const cells = line
        .split('|')
        .slice(1, -1)
        .map((c) => c.trim())
      if (!/^-+$/.test(cells[0] ?? '')) rows.push(cells)
    } else if (inTable) break
  }
  return rows.slice(1)
}

const code = (cell: string) => /`([^`]+)`/.exec(cell)?.[1] ?? cell

/** `/chat/:agent/*` -> `/chat/<agent>/<rest of key>` style, for comparison. */
function docPattern(path: string): string {
  return `/${path}`.replace(/:[a-zA-Z]+|\*/g, '<>')
}

describe('docs/web-ui.md routes', () => {
  it('lists exactly the routes in the route table', () => {
    const documented = tableAfter('### Routes')
      .map((r) => code(r[0]!))
      .map((p) => p.replace(/<[^>]+>/g, '<>'))
      .filter((p) => p !== '/' && p !== '/mcp/oauth/callback')
    expect(documented.sort()).toEqual(DESTINATION_ROUTES.map((r) => docPattern(r.path!)).sort())
    expect(tableAfter('### Routes').map((r) => code(r[0]!))).toContain('/mcp/oauth/callback')
  })

  it('documents every legacy redirect and its target', () => {
    const rows = tableAfter('### Legacy URLs').map(([from, to]) => [code(from!), code(to!)])
    const heads = new Set(rows.map(([from]) => from!.split('?')[0]!.slice(1)))
    for (const path of LEGACY_PATHS) expect(heads, path).toContain(path)
    const sample: Record<string, string> = {
      '<id>': 'P',
      '<key>': 'agent:main:webchat:abc',
      '<agent>': 'main',
      '<step>': 'router',
    }
    const fill = (s: string) => s.replace(/<[^>]+>/g, (m) => sample[m] ?? m)
    for (const [from, to] of rows) {
      const [path, query = ''] = fill(from!).split('?')
      const expected = fill(to!).replace('<rest of key>', 'webchat/abc')
      expect(legacyRedirect(path!, query ? `?${query}` : ''), from).toBe(expected)
    }
  })
})
