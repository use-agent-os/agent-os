// Fonts come from the console bundle, never a third-party host
// (design-system spec "Typography" / "No external font requests").
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

const STYLES = 'src/web/styles'
const DIST = '../src/agentos/gateway/static/dist'
const EXTERNAL_FONT =
  /(fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit|url\(\s*['"]?(https?:)?\/\/)/i

function files(dir: string, suffixes: string[]): string[] {
  if (!existsSync(dir)) return []
  return readdirSync(dir, { recursive: true, withFileTypes: true })
    .filter((e) => e.isFile() && suffixes.some((s) => e.name.endsWith(s)))
    .map((e) => path.join(e.parentPath, e.name))
}

describe('bundled fonts', () => {
  const base = readFileSync(path.join(STYLES, 'base.css'), 'utf8')

  it('declares Inter and JetBrains Mono from bundled files', () => {
    const faces = [...base.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((m) => m[1]!)
    const families = faces.map((f) => /font-family:\s*'([^']+)'/.exec(f)?.[1])
    expect(families).toEqual(['Inter Variable', 'JetBrains Mono Variable'])
    for (const face of faces) {
      const url = /url\('([^']+)'\)/.exec(face)?.[1]
      expect(url).toBeDefined()
      expect(existsSync(path.join(STYLES, url!))).toBe(true)
    }
  })

  it('uses Inter for the interface and JetBrains Mono for code', () => {
    const tokens = readFileSync(path.join(STYLES, 'tokens.css'), 'utf8')
    expect(tokens).toMatch(/--font-sans:\s*'Inter Variable'/)
    expect(tokens).toMatch(/--font-mono:\s*'JetBrains Mono Variable'/)
    expect(base).toMatch(/code,\s*kbd,\s*samp,\s*pre\s*\{\s*font-family: var\(--font-mono\)/)
  })

  it('references no third-party font host in the web stylesheets', () => {
    for (const file of files('src/web', ['.css', '.ts', '.tsx'])) {
      if (file.endsWith('.test.ts')) continue
      expect(readFileSync(file, 'utf8'), file).not.toMatch(EXTERNAL_FONT)
    }
  })

  // The built bundle is gitignored; when a build is present (CI builds before
  // testing in the release gate) check it too.
  it.runIf(existsSync(path.join(DIST, 'index.html')))(
    'ships no third-party font URL in the built dist',
    () => {
      for (const file of files(DIST, ['.css', '.html', '.js'])) {
        expect(readFileSync(file, 'utf8'), file).not.toMatch(
          /fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit\.net/i,
        )
      }
    },
  )
})
