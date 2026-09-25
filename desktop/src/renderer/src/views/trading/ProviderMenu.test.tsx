import { readFileSync } from 'node:fs'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ProviderMenu } from './ProviderMenu'
import type { ProviderId, ProviderStatus } from './types'

const PROVIDERS: ProviderStatus[] = [
  {
    id: 'aggregator',
    label: 'AgentOS Aggregator',
    needsKey: false,
    keyConfigured: true,
    healthy: null,
  },
  { id: 'uniswap', label: 'Uniswap', needsKey: true, keyConfigured: false, healthy: null },
]

function mount(opts: { provider?: ProviderId; switching?: boolean } = {}) {
  const onSwitch = vi.fn()
  const onOpenSettings = vi.fn()
  const view = (switching: boolean) => (
    <ProviderMenu
      className="trd-venue"
      testId="provider-pill"
      provider={opts.provider ?? 'aggregator'}
      providers={PROVIDERS}
      switching={switching}
      onSwitch={onSwitch}
      onOpenSettings={onOpenSettings}
    >
      face
    </ProviderMenu>
  )
  const { rerender } = render(view(Boolean(opts.switching)))
  return {
    onSwitch,
    onOpenSettings,
    trigger: screen.getByTestId('provider-pill'),
    setSwitching: (switching: boolean) => rerender(view(switching)),
  }
}

describe('ProviderMenu · the route as a pop-up button', () => {
  it('is a menu button named by the route, not only by its face', () => {
    const { trigger } = mount({ provider: 'uniswap' })
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    // The seat's face can be squeezed down to its glyph; the name keeps the route.
    expect(trigger).toHaveAccessibleName('Swap provider: Uniswap')
  })

  it('lists every provider, checks the current one and says what each needs', () => {
    const { trigger } = mount({ provider: 'uniswap' })
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    const rows = screen.getAllByRole('menuitemradio')
    expect(rows.map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    expect(rows[0]).toHaveTextContent('AgentOS Aggregator')
    expect(rows[0]).toHaveTextContent('no key')
    expect(rows[1]).toHaveTextContent('Uniswap')
    expect(rows[1]).toHaveTextContent('needs an API key')
  })

  it('switches to another route, and picking the current one changes nothing', () => {
    const { trigger, onSwitch, onOpenSettings } = mount({ provider: 'aggregator' })
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^AgentOS Aggregator/ }))
    expect(screen.queryByRole('menu')).toBeNull()
    expect(onSwitch).not.toHaveBeenCalled()

    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^Uniswap/ }))
    expect(onSwitch).toHaveBeenCalledTimes(1)
    expect(onSwitch).toHaveBeenCalledWith('uniswap')

    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Provider settings…' }))
    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })

  it('opens from the arrow keys and gives focus back to the button on Escape', () => {
    const { trigger } = mount()
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    const first = screen.getByRole('menuitemradio', { name: /^AgentOS Aggregator/ })
    expect(document.activeElement).toBe(first)

    fireEvent.keyDown(first, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    // Focus left with the menu would drop to the page, and Tab would start over.
    expect(document.activeElement).toBe(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('gives focus back to the button after a pick from the keyboard', () => {
    const { trigger, onSwitch } = mount()
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })
    const uniswap = screen.getByRole('menuitemradio', { name: /^Uniswap/ })
    expect(document.activeElement).toBe(uniswap)
    // Return on a focused row is a click.
    fireEvent.click(uniswap)
    expect(onSwitch).toHaveBeenCalledWith('uniswap')
    expect(document.activeElement).toBe(trigger)
  })

  it('waits out a switch in flight without letting go of focus', () => {
    const { trigger, setSwitching } = mount()
    trigger.focus()
    setSwitching(true)
    // `disabled` would make Chromium drop the focus the pick just handed back.
    expect(trigger).toBeEnabled()
    expect(trigger).toHaveAttribute('aria-disabled', 'true')
    expect(document.activeElement).toBe(trigger)
    // …and it takes no second switch until the first has landed.
    fireEvent.click(trigger)
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    expect(screen.queryByRole('menu')).toBeNull()

    setSwitching(false)
    expect(trigger).not.toHaveAttribute('aria-disabled')
    fireEvent.click(trigger)
    expect(screen.getByRole('menu', { name: 'Swap provider' })).toBeInTheDocument()
  })

  it('keeps both faces lit while their menu is open', () => {
    // Read as text: vitest stubs CSS imports, so this asserts what the sheets say.
    const sheets = [
      readFileSync('src/renderer/src/views/trading/trading.css', 'utf8'),
      readFileSync('src/renderer/src/views/trading/desk/desk.css', 'utf8'),
    ].join('\n')
    for (const face of ['.trd-venue', '.trd-seat']) {
      expect(sheets).toContain(`${face}[aria-expanded='true']`)
    }
  })
})
