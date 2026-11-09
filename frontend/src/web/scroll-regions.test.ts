// A box that scrolls plain text (a <pre>, a <code> block, a log) has nothing
// inside it to Tab to, so keyboard users can only scroll it if the box itself
// takes focus (WCAG 2.1.1; axe `scrollable-region-focusable`). Found by the
// task 10.2 UI audit (tests/functional/webui_ui_audit.cjs).
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

const WEB_DIR = 'src/web'
const SCROLLS = /\boverflow-(?:auto|x-auto|y-auto)\b|\bscroll-contained\b/

function sources(): { file: string; text: string }[] {
  return readdirSync(WEB_DIR, { recursive: true, withFileTypes: true })
    .filter((e) => e.isFile() && e.name.endsWith('.tsx') && !e.name.endsWith('.test.tsx'))
    .map((e) => path.join(e.parentPath, e.name))
    .map((file) => ({ file, text: readFileSync(file, 'utf8') }))
}

/** Opening tags of text-only scroll boxes that cannot take focus. */
function unfocusableTextScrollers(text: string): string[] {
  const found: string[] = []
  const tags = /<(pre|code|div)\b((?:[^<>]|=>)*?)>/g
  for (const m of text.matchAll(tags)) {
    const [tag, name, attrs] = m as unknown as [string, string, string]
    if (!SCROLLS.test(attrs) || /tabIndex=\{0\}/.test(attrs)) continue
    // Divs only count when they are logs: everything else holds controls.
    if (name === 'div' && !/role="log"/.test(attrs)) continue
    found.push(tag.replace(/\s+/g, ' ').slice(0, 120))
  }
  return found
}

describe('text scroll boxes', () => {
  it('detects a <pre> that scrolls without taking focus', () => {
    expect(unfocusableTextScrollers('<pre className="max-h-64 overflow-auto">')).toHaveLength(1)
    expect(unfocusableTextScrollers('<pre tabIndex={0} className="overflow-auto">')).toEqual([])
    expect(unfocusableTextScrollers('<div className="overflow-y-auto">')).toEqual([])
    expect(
      unfocusableTextScrollers('<div role="log" className="h-72 overflow-y-auto">'),
    ).toHaveLength(1)
  })

  it('lets every scrolling <pre>, <code> block and log take keyboard focus', () => {
    const offenders = sources().flatMap(({ file, text }) =>
      unfocusableTextScrollers(text).map((tag) => `${file}: ${tag}`),
    )
    expect(offenders).toEqual([])
  })
})
