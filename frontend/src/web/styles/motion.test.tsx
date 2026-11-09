// Motion baseline (design-system spec "Motion", "Forced colors"): durations
// capped at 300 ms, reduced motion makes dialogs and the drawer instant, and
// forced-colors keeps controls, focus and status visible.
import '@/test/web'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from '../components/ui/dialog'
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from '../components/ui/sheet'

const baseCss = readFileSync('src/web/styles/base.css', 'utf8')
const tokensCss = readFileSync('src/web/styles/tokens.css', 'utf8')

function mediaBlock(css: string, query: string): string {
  const start = css.indexOf(`@media ${query}`)
  if (start < 0) return ''
  let depth = 0
  for (let i = css.indexOf('{', start); i < css.length; i++) {
    if (css[i] === '{') depth++
    if (css[i] === '}' && --depth === 0) return css.slice(start, i + 1)
  }
  return ''
}

/** Every anim-* class used by a rendered element must be defined in base.css. */
function animationClasses(el: Element): string[] {
  return [...el.classList].filter((c) => c.startsWith('anim-') || /^(animate|transition)-/.test(c))
}

describe('motion', () => {
  it('caps every duration token at 300 ms', () => {
    const durations = [...tokensCss.matchAll(/--duration-[\w-]+:\s*(\d+)ms/g)].map((m) =>
      Number(m[1]),
    )
    expect(durations.length).toBeGreaterThanOrEqual(3)
    expect(Math.max(...durations)).toBeLessThanOrEqual(300)
    // No literal duration in the web stylesheets bypasses the tokens. The
    // reduced-motion block (0s overrides, the status pulse) is checked below.
    const rest = baseCss.replace(mediaBlock(baseCss, '(prefers-reduced-motion: reduce)'), '')
    expect(rest).not.toMatch(/\b(animation|transition)[^;]*\b\d+(\.\d+)?m?s\b(?![^;]*var\()/)
  })

  it('never makes an exit slower than its entrance', () => {
    const rule = (name: string, state: string) =>
      new RegExp(
        `\\.${name}\\[data-state='${state}'\\]\\s*\\{[^}]*var\\(--duration-(\\w+)\\)`,
      ).exec(baseCss)?.[1]
    const rank = { fast: 0, base: 1, slow: 2 } as Record<string, number>
    for (const name of ['anim-overlay-in', 'anim-dialog-in', 'anim-drawer-in']) {
      const enter = rule(name, 'open')
      const exit = rule(name, 'closed')
      expect(enter, name).toBeDefined()
      expect(rank[exit!]!, name).toBeLessThanOrEqual(rank[enter!]!)
    }
  })

  it('makes every animation and transition instant under reduced motion', () => {
    const block = mediaBlock(baseCss, '(prefers-reduced-motion: reduce)')
    expect(block).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{/)
    expect(block).toContain('animation-duration: 0s !important')
    expect(block).toContain('transition-duration: 0s !important')
    expect(block).toContain('scroll-behavior: auto !important')
  })

  it('animates the dialog and drawer only through classes reduced motion neutralises', async () => {
    const user = userEvent.setup()
    render(
      <>
        <Dialog>
          <DialogTrigger>Open dialog</DialogTrigger>
          <DialogContent>
            <DialogTitle>Dialog</DialogTitle>
          </DialogContent>
        </Dialog>
        <Sheet>
          <SheetTrigger>Open drawer</SheetTrigger>
          <SheetContent>
            <SheetTitle>Drawer</SheetTitle>
          </SheetContent>
        </Sheet>
      </>,
    )
    await user.click(screen.getByText('Open dialog'))
    const dialog = await screen.findByRole('dialog', { name: 'Dialog' })
    const classes = [
      ...animationClasses(dialog),
      ...animationClasses(document.querySelector('[data-slot="dialog-overlay"]')!),
    ]
    await user.keyboard('{Escape}')
    await user.click(screen.getByText('Open drawer'))
    const drawer = await screen.findByRole('dialog', { name: 'Drawer' })
    classes.push(...animationClasses(drawer))
    expect(classes).toContain('anim-dialog-in')
    expect(classes).toContain('anim-drawer-in')
    for (const c of classes) {
      // Only CSS animations (covered by the `*` reduced-motion rule) — no
      // JS-driven motion that the media query could not stop.
      expect(c.startsWith('anim-'), c).toBe(true)
      expect(baseCss, c).toContain(`.${c}[data-state='open']`)
    }
  })

  it('keeps controls, focus and status visible in forced colors', () => {
    const block = mediaBlock(baseCss, '(forced-colors: active)')
    expect(block).toContain(':focus-visible')
    expect(block).toMatch(/button,[\s\S]*border: 1px solid ButtonText/)
    expect(block).toContain('[data-status-icon]')
    expect(block).toMatch(/\[data-decorative\]\s*\{\s*display: none/)
  })
})
