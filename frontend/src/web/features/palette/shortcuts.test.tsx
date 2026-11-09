import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderShell, resetShellState } from '@/test/web-shell'
import { useShell } from '@/web/shell/shell-store'

beforeEach(resetShellState)
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

async function ready() {
  return screen.findByRole('navigation', { name: 'Main navigation' })
}

function composer() {
  const el = document.createElement('textarea')
  el.setAttribute('aria-label', 'Message')
  document.body.appendChild(el)
  act(() => el.focus())
  return el
}

describe('global shortcuts', () => {
  it('inserts ? as text in the composer instead of opening the overview', async () => {
    const user = userEvent.setup()
    renderShell()
    await ready()
    const el = composer()
    await user.keyboard('?')
    expect(el).toHaveValue('?')
    expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument()
    el.remove()
  })

  it('opens the palette with Ctrl+K from the composer', async () => {
    const user = userEvent.setup()
    renderShell()
    await ready()
    const el = composer()
    await user.keyboard('{Control>}k{/Control}')
    expect(await screen.findByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
    el.remove()
  })

  it('opens the overview with ? and with Ctrl+/', async () => {
    const user = userEvent.setup()
    renderShell()
    await ready()
    await user.keyboard('?')
    expect(await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.keyboard('{Control>}/{/Control}')
    expect(await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument()
  })

  it('navigates to /new with Mod+Shift+O', async () => {
    const user = userEvent.setup()
    const { router } = renderShell()
    await ready()
    await user.keyboard('{Control>}{Shift>}o{/Shift}{/Control}')
    expect(router.state.location.pathname).toBe('/new')
  })

  it('toggles the list pane with Mod+B', async () => {
    const user = userEvent.setup()
    renderShell()
    await ready()
    await user.keyboard('{Control>}b{/Control}')
    expect(useShell.getState().paneOpen).toBe(false)
  })
})

describe('navigation chords', () => {
  it.each([
    ['n', '/settings/channels'],
    ['o', '/home'],
    ['r', '/automations'],
    [',', '/settings'],
    ['c', '/chat'],
  ])('g %s goes to %s', async (key, path) => {
    const user = userEvent.setup()
    const { router } = renderShell('/usage')
    await ready()
    await user.keyboard(`g${key}`)
    await waitFor(() => expect(router.state.location.pathname).toBe(path))
  })

  it('does nothing when the second key comes after 2 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const { router } = renderShell('/usage')
    await ready()
    await user.keyboard('g')
    act(() => vi.advanceTimersByTime(2_000))
    await user.keyboard('n')
    expect(router.state.location.pathname).toBe('/usage')
  })

  it('is ignored while typing and while a dialog is open', async () => {
    const user = userEvent.setup()
    const { router } = renderShell('/usage')
    await ready()
    const el = composer()
    await user.keyboard('gn')
    expect(router.state.location.pathname).toBe('/usage')
    el.remove()
    await user.keyboard('?')
    await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    await user.keyboard('gn')
    expect(router.state.location.pathname).toBe('/usage')
  })
})

describe('shortcut overview', () => {
  function stubPlatform(platform: string) {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform)
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(platform)
  }

  it('labels the modifier Ctrl on Linux', async () => {
    stubPlatform('Linux x86_64')
    const user = userEvent.setup()
    renderShell()
    await ready()
    await user.keyboard('?')
    const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    const global = within(dialog).getByRole('region', { name: 'Global' })
    expect(global).toHaveTextContent('Ctrl')
    expect(global).not.toHaveTextContent('⌘')
    expect(within(dialog).getByRole('region', { name: 'Navigation' })).toHaveTextContent(
      'Go to Channels settings',
    )
    expect(within(dialog).getByRole('region', { name: 'Composer' })).toHaveTextContent('New line')
  })

  it('labels the modifier ⌘ on macOS', async () => {
    stubPlatform('MacIntel')
    const user = userEvent.setup()
    renderShell()
    await ready()
    await user.keyboard('?')
    const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(within(dialog).getByRole('region', { name: 'Global' })).toHaveTextContent('⌘')
  })
})
