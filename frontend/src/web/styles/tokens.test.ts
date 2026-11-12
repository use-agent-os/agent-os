// Token contract (overhaul-control-ui D3, design-system spec): every declared
// text, control-boundary and focus pair meets its WCAG threshold in both themes.
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'

const tokensCss = readFileSync('src/web/styles/tokens.css', 'utf8')

type Theme = 'dark' | 'light'
type Tokens = Record<string, string>

const THEME_SELECTORS: Record<Theme, RegExp> = {
  dark: /:root,\s*:root\[data-theme='dark'\]\s*\{([^}]*)\}/,
  light: /:root\[data-theme='light'\]\s*\{([^}]*)\}/,
}

function parseTheme(css: string, theme: Theme): Tokens {
  const block = THEME_SELECTORS[theme].exec(css)?.[1]
  if (!block) throw new Error(`no ${theme} theme block`)
  const raw: Tokens = {}
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    raw[match[1]!] = match[2]!.replace(/\/\*.*?\*\//g, '').trim()
  }
  const resolve = (value: string, depth = 0): string => {
    const ref = /^var\((--[\w-]+)\)$/.exec(value)
    if (!ref || depth > 4) return value
    return resolve(raw[ref[1]!] ?? value, depth + 1)
  }
  return Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, resolve(v)]))
}

function luminance(hex: string): number {
  const m = /^#([0-9a-f]{6})$/i.exec(hex)
  if (!m) throw new Error(`not a #rrggbb colour: ${hex}`)
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(m[1]!.slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi! + 0.05) / (lo! + 0.05)
}

const SURFACES = [
  '--surface-0',
  '--surface-1',
  '--surface-2',
  '--surface-3',
  '--surface-inset',
  '--surface-overlay',
]
// Controls and focus rings sit on the canvas, panes, cards, input wells and
// overlays; surface-3 is a hover/selected fill that never hosts a bare input
// boundary.
const CONTROL_SURFACES = SURFACES.filter((s) => s !== '--surface-3')
const TEXT_TOKENS = ['--text', '--text-muted', '--accent', '--ok', '--warn', '--danger', '--info']
const FILL_PAIRS: [string, string][] = [
  ['--accent-fg', '--accent'],
  ['--ok-fg', '--ok'],
  ['--warn-fg', '--warn'],
  ['--danger-fg', '--danger'],
  ['--info-fg', '--info'],
]

// Syntax-highlight colours on the code-block background (chat spec "Code
// block in light theme").
const CODE_TOKENS = [
  '--code-text',
  '--hl-keyword',
  '--hl-string',
  '--hl-number',
  '--hl-comment',
  '--hl-title',
  '--hl-attr',
  '--hl-builtin',
]

interface Violation {
  pair: string
  ratio: number
  min: number
}

function contractViolations(css: string, theme: Theme): Violation[] {
  const t = parseTheme(css, theme)
  const out: Violation[] = []
  const check = (fg: string, bg: string, min: number) => {
    const ratio = contrast(t[fg]!, t[bg]!)
    if (ratio < min) out.push({ pair: `${fg} on ${bg}`, ratio, min })
  }
  for (const fg of TEXT_TOKENS) for (const bg of SURFACES) check(fg, bg, 4.5)
  for (const fg of ['--border-control', '--ring'])
    for (const bg of CONTROL_SURFACES) check(fg, bg, 3)
  for (const [fg, bg] of FILL_PAIRS) check(fg, bg, 4.5)
  for (const fg of CODE_TOKENS) check(fg, '--code-bg', 4.5)
  return out
}

describe('design tokens', () => {
  for (const theme of ['dark', 'light'] as const) {
    it(`${theme} theme meets every WCAG pair`, () => {
      expect(contractViolations(tokensCss, theme)).toEqual([])
    })
  }

  it('declares the same token names in both themes', () => {
    expect(Object.keys(parseTheme(tokensCss, 'light')).sort()).toEqual(
      Object.keys(parseTheme(tokensCss, 'dark')).sort(),
    )
  })

  it('uses lime on dark and olive on light as the accent', () => {
    expect(parseTheme(tokensCss, 'dark')['--accent']).toBe('#ccff00')
    expect(parseTheme(tokensCss, 'light')['--accent']).toBe('#4d6600')
  })

  it('fails when one token drops below its threshold', () => {
    const broken = tokensCss.replace('--text-muted: #5b616b;', '--text-muted: #a0a4aa;')
    expect(broken).not.toBe(tokensCss)
    const violations = contractViolations(broken, 'light')
    expect(violations.map((v) => v.pair)).toContain('--text-muted on --surface-0')
    const ring = tokensCss.replace('--border-control: #68707b;', '--border-control: #3a3f46;')
    expect(
      contractViolations(ring, 'dark').some((v) => v.pair.startsWith('--border-control')),
    ).toBe(true)
  })

  it('defines the three text-size scales', () => {
    expect(tokensCss).toMatch(/\[data-text-size='small'\]\s*\{\s*--text-scale:\s*0\.93/)
    expect(tokensCss).toMatch(/\[data-text-size='large'\]\s*\{\s*--text-scale:\s*1\.1/)
  })
})
