import { readFileSync } from 'node:fs'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RoutePicker } from '@/views/chat/RoutePicker'
import type { RoutePinApi } from '@/views/chat/useRoutePin'

// The route picker is the console's component, but the console's stylesheet is
// never loaded here: chat.css restyles its classes one by one, and a class it
// misses renders as bare text. That is how the vision-tier footer came out as
// `image_modelglm-5.3-flash` with no separator, run straight into the list's
// half-clipped last row. Comments are dropped so prose that names a class
// cannot stand in for a rule.
const css = readFileSync('src/renderer/src/views/chat/chat.css', 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
)

/** The declarations of the rule whose selector is exactly `selector`. */
function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = new RegExp(`(?:^|\\n)${escaped} \\{([^}]*)\\}`).exec(css)
  if (!m?.[1]) throw new Error(`chat.css has no \`${selector}\` rule`)
  return m[1]
}

function px(decls: string, prop: string): number {
  const m = new RegExp(`(?:^|[\\s;])${prop}: (\\d+(?:\\.\\d+)?)px;`).exec(decls)
  if (!m?.[1]) throw new Error(`no px \`${prop}\` in ${decls}`)
  return Number(m[1])
}

/** The left edge of a `padding` shorthand, whichever of its four forms. */
function paddingLeft(decls: string): number {
  const values = /(?:^|[\s;])padding: ([^;]+);/.exec(decls)?.[1]?.trim().split(/\s+/) ?? []
  const left = values.length === 4 ? values[3] : values.length > 1 ? values[1] : values[0]
  if (left === undefined) throw new Error(`no padding in ${decls}`)
  return parseFloat(left)
}

/** The right edge of a `padding` shorthand, whichever of its four forms. */
function paddingRight(decls: string): number {
  const values = /(?:^|[\s;])padding: ([^;]+);/.exec(decls)?.[1]?.trim().split(/\s+/) ?? []
  const right = values.length > 1 ? values[1] : values[0]
  if (right === undefined) throw new Error(`no padding in ${decls}`)
  return parseFloat(right)
}

const tokens = readFileSync('src/renderer/src/theme/tokens.css', 'utf8')

function route(): RoutePinApi {
  return {
    enabled: true,
    tiers: [
      { tier: 'c0', model: 'deepseek-v4.1-flash' },
      { tier: 'c2', model: 'glm-5.3' },
    ],
    // Not a pinnable text tier, so the picker reports it below the list.
    imageTiers: [{ tier: 'image_model', model: 'glm-5.3-flash' }],
    models: [{ id: 'gpt-5-mini', name: 'GPT-5 Mini' }],
    pinned: null,
    pinnedModel: null,
    isPinned: false,
    lastRoutedTier: null,
    lastRoutedModel: null,
    imageOverride: false,
    busy: false,
    pin: vi.fn(),
    pinModel: vi.fn(),
    clear: vi.fn(),
    reload: vi.fn(),
  }
}

function openMenu(): HTMLElement {
  const { container } = render(<RoutePicker route={route()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Model route' }))
  const menu = container.querySelector<HTMLElement>('.chat-route-menu')
  if (!menu) throw new Error('the route menu did not open')
  return menu
}

/** The picker's own classes under `root` (the icons bring lucide's along). */
const classesIn = (root: HTMLElement): string[] =>
  [root, ...root.querySelectorAll('[class]')]
    .flatMap((el) => [...el.classList])
    .filter((cls) => cls.startsWith('chat-route'))

describe('desktop skin for the shared route picker', () => {
  it('restyles every class the open menu renders, the vision footer included', () => {
    const menu = openMenu()
    expect(menu.querySelector('.chat-route-image__row')).toHaveTextContent('image_model')
    const rendered = new Set(classesIn(menu))
    // A search that matches nothing swaps the rows for the empty note.
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'no such route' } })
    for (const cls of classesIn(menu)) rendered.add(cls)
    expect(rendered).toContain('chat-route-empty')

    const selectors = css.match(/[^{}]+(?=\{)/g) ?? []
    const styled = new Set(selectors.flatMap((s) => s.match(/(?<=\.)[\w-]+/g) ?? []))
    expect([...rendered].filter((cls) => !styled.has(cls))).toEqual([])
  })

  it('keeps the footer inside the menu, below a list that is the only part to give way', () => {
    const menu = openMenu()
    // The markup half of the contract: search, then the listbox, then the footer.
    expect([...menu.children].map((el) => el.className)).toEqual([
      'chat-route-search',
      'chat-route-list',
      'chat-route-image',
    ])

    const panel = rule('.chat-route-menu')
    expect(panel).toMatch(/display: flex;/)
    expect(panel).toMatch(/flex-direction: column;/)
    expect(panel).toMatch(/max-height: calc\(100vh - [\d.]+rem\);/)
    // Before the first send the composer sits mid-pane, so the menu gets at
    // most half the window or <main> clips its top edge, search box first.
    const undocked = rule(".chat-desktop[data-docked='false'] .chat-route-menu")
    const share = Number(/max-height: calc\((\d+)vh - [\d.]+rem\);/.exec(undocked)?.[1])
    expect(share).toBeGreaterThan(0)
    expect(share).toBeLessThanOrEqual(50)

    const list = rule('.chat-route-list')
    expect(list).toMatch(/min-height: 0;/)
    expect(list).toMatch(/overflow: auto;/)
    expect(rule('.chat-route-search')).toMatch(/flex-shrink: 0;/)
    const footer = rule('.chat-route-image')
    expect(footer).toMatch(/flex-shrink: 0;/)
    expect(footer).toMatch(/border-top: 1px solid var\(--hairline\);/)
  })

  it('lines the footer up with the options and keeps tier and model apart', () => {
    const option = rule('.chat-route-option')
    const row = rule('.chat-route-image__row')
    expect(row).toMatch(/display: flex;/)
    expect(px(row, 'gap')).toBeGreaterThan(0)
    // Tier names start where the options' tier names do, past the check column.
    expect(paddingLeft(row)).toBe(
      paddingLeft(option) + px(rule('.chat-route-option__check'), 'width') + px(option, 'gap'),
    )
    expect(paddingLeft(rule('.chat-route-image__hint'))).toBe(paddingLeft(option))
    // Models end where the options' models do: the list keeps its scrollbar's
    // gutter whether or not it scrolls, and the footer, outside the list,
    // pads its right edge by the option's padding plus that gutter.
    expect(rule('.chat-route-list')).toMatch(/scrollbar-gutter: stable;/)
    const scrollbar = Number(/::-webkit-scrollbar \{\s*width: (\d+)px;/.exec(tokens)?.[1])
    expect(scrollbar).toBeGreaterThan(0)
    expect(paddingRight(row)).toBe(paddingRight(option) + scrollbar)

    // The footer does not scroll like the list, so a long model id has to
    // truncate rather than run out past the menu's edge.
    const model = rule('.chat-route-image__model')
    expect(model).toMatch(/min-width: 0;/)
    expect(model).toMatch(/overflow: hidden;/)
    expect(model).toMatch(/text-overflow: ellipsis;/)
    expect(model).toMatch(/white-space: nowrap;/)
  })
})
