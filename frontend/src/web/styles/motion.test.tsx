// Motion baseline (design-system spec "Motion", "Forced colors"): interface
// transitions capped at 300 ms and entrance choreography (duration plus capped
// stagger) at 450 ms, exits never slower than entrances, reduced motion makes
// dialogs, the drawer and entrances instant, and forced-colors keeps controls,
// focus and status visible.
import '@/test/web'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from '@/web/components/ui/dialog'
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from '@/web/components/ui/sheet'

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
  it('caps interface transitions at 300 ms and entrance choreography at 450 ms', () => {
    const durations = Object.fromEntries(
      [...tokensCss.matchAll(/--duration-([\w-]+):\s*(\d+)ms/g)].map((m) => [m[1]!, Number(m[2])]),
    )
    expect(Object.keys(durations).length).toBeGreaterThanOrEqual(3)
    // Entrances (page/section arrival, spring pops) may run longer; nothing
    // else does.
    const ENTRANCE = ['enter', 'spring']
    for (const [name, ms] of Object.entries(durations)) {
      expect(ms, name).toBeLessThanOrEqual(ENTRANCE.includes(name) ? 450 : 300)
    }
    // A staggered list finishes within the entrance budget: the last staggered
    // child starts after 5 steps (.anim-stagger caps the index at 5).
    const step = Number(/--stagger-step:\s*(\d+)ms/.exec(tokensCss)?.[1])
    expect(step).toBeGreaterThan(0)
    expect(baseCss).toMatch(/var\(--stagger-i, 5\)/)
    expect(durations.enter! + 5 * step).toBeLessThanOrEqual(450)
    // No literal duration in the web stylesheets bypasses the tokens. The
    // reduced-motion block (0s overrides, the status pulse) is checked below.
    const rest = baseCss.replace(mediaBlock(baseCss, '(prefers-reduced-motion: reduce)'), '')
    expect(rest).not.toMatch(/\b(animation|transition)[^;]*\b\d+(\.\d+)?m?s\b(?![^;]*var\()/)
  })

  it('never makes an exit slower than its entrance', () => {
    const ms = (token: string) =>
      Number(new RegExp(`--duration-${token}:\\s*(\\d+)ms`).exec(tokensCss)?.[1])
    const rule = (name: string, state: string) =>
      new RegExp(
        `\\.${name}\\[data-state='${state}'\\]\\s*\\{[^}]*var\\(--duration-(\\w+)\\)`,
      ).exec(baseCss)?.[1]
    for (const name of [
      'anim-overlay-in',
      'anim-dialog-in',
      'anim-drawer-in',
      'anim-sheet-right',
      'anim-sheet-bottom',
      'anim-pop',
    ]) {
      const enter = rule(name, 'open') ?? (name === 'anim-pop' ? 'slow' : undefined)
      const exit = rule(name, 'closed')
      expect(enter, name).toBeDefined()
      expect(exit, name).toBeDefined()
      expect(ms(exit!), name).toBeLessThanOrEqual(ms(enter!))
    }
  })

  it('makes every animation and transition instant under reduced motion', () => {
    const block = mediaBlock(baseCss, '(prefers-reduced-motion: reduce)')
    expect(block).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{/)
    expect(block).toContain('animation-duration: 0s !important')
    expect(block).toContain('transition-duration: 0s !important')
    expect(block).toContain('scroll-behavior: auto !important')
    // Nothing waits invisibly on a stagger delay, and page transitions stop.
    expect(block).toContain('animation-delay: 0s !important')
    expect(block).toMatch(/::view-transition-new\(\*\)\s*\{\s*animation: none !important/)
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
