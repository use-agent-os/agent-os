import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { SessionChip } from './SessionChip'
import type { SessionListItem } from './logic'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const CURRENT = 'agent:main:webchat:default'

const SESSIONS: SessionListItem[] = [
  'agent:main:webchat:default',
  'agent:main:webchat:abc123',
  { key: 'agent:trader:cli:default', run_status: 'running' },
  'sess-legacy',
]

function renderChip(overrides: Partial<Parameters<typeof SessionChip>[0]> = {}) {
  const onSwitch = vi.fn()
  const onReset = vi.fn()
  const onCopy = vi.fn().mockResolvedValue(undefined)
  const fetchSessions = vi.fn().mockResolvedValue(SESSIONS)
  render(
    <SessionChip
      sessionKey={CURRENT}
      onSwitch={onSwitch}
      onReset={onReset}
      onCopy={onCopy}
      fetchSessions={fetchSessions}
      {...overrides}
    />,
  )
  return { onSwitch, onReset, onCopy, fetchSessions }
}

afterEach(() => {
  sessionStorage.clear()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('SessionChip', () => {
  it('renders the current session key on the chip (chat.js:1223)', () => {
    renderChip()
    const chip = screen.getByRole('button', { name: /switch chat session/i })
    expect(chip).toHaveTextContent(CURRENT)
  })

  it('opens the switcher popover on click and lists the fetched sessions (chat.js:2026/2071)', async () => {
    renderChip()
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    // The popover dialog appears; the fetched session keys are listed.
    expect(await screen.findByRole('dialog', { name: /switch session/i })).toBeInTheDocument()
    expect(await screen.findByText('agent:main:webchat:abc123')).toBeInTheDocument()
    expect(screen.getByText('agent:trader:cli:default')).toBeInTheDocument()
  })

  it('authenticates the default session-list request with the per-tab gateway token', async () => {
    sessionStorage.setItem('agentos.wsToken', 'session-token')
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ sessions: SESSIONS }),
    } as Response)
    vi.stubGlobal('fetch', fetchSpy)
    renderChip({ fetchSessions: undefined })

    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    expect(await screen.findByText('agent:main:webchat:abc123')).toBeInTheDocument()
    expect(fetchSpy).toHaveBeenCalledWith('/api/sessions', {
      headers: { Authorization: 'Bearer session-token' },
      credentials: 'same-origin',
    })
  })

  it('closes the switcher on Escape and restores focus to its trigger', async () => {
    renderChip()
    const trigger = screen.getByRole('button', { name: /switch chat session/i })
    fireEvent.click(trigger)
    expect(await screen.findByRole('dialog', { name: /switch session/i })).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog', { name: /switch session/i })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('groups sessions and tags a running one with its run status (chat.js:1862/1611)', async () => {
    renderChip()
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    // The webchat group label + the CLI group label both render.
    expect(await screen.findByText('Web chat')).toBeInTheDocument()
    expect(screen.getByText('CLI')).toBeInTheDocument()
    // The running CLI session shows a Running run-status tag (chat.js:1934).
    expect(screen.getByText('Running')).toBeInTheDocument()
  })

  it('marks the current session and switching to it is a no-op (chat.js:1938/1946)', async () => {
    const { onSwitch } = renderChip()
    const trigger = screen.getByRole('button', { name: /switch chat session/i })
    fireEvent.click(trigger)
    // The current row carries the "current" tag.
    expect(await screen.findByText('current')).toBeInTheDocument()
    // Clicking the current session must NOT fire onSwitch (chat.js:1946 `k !== current`).
    fireEvent.click(screen.getByText(CURRENT, { selector: '.chat-session-popover-item-key' }))
    expect(onSwitch).not.toHaveBeenCalled()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('switching to a different session fires onSwitch with its key (chat.js:1946)', async () => {
    const { onSwitch } = renderChip()
    const trigger = screen.getByRole('button', { name: /switch chat session/i })
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByText('agent:main:webchat:abc123'))
    expect(onSwitch).toHaveBeenCalledWith('agent:main:webchat:abc123')
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('filters the list by the search input (chat.js:1911/2072)', async () => {
    renderChip()
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    await screen.findByText('agent:main:webchat:abc123')
    const search = screen.getByRole('searchbox', { name: /search sessions/i })
    fireEvent.change(search, { target: { value: 'abc' } })
    await waitFor(() => {
      expect(screen.getByText('agent:main:webchat:abc123')).toBeInTheDocument()
      expect(screen.queryByText('agent:trader:cli:default')).not.toBeInTheDocument()
    })
  })

  it('copies the session key and toasts (chat.js:1782/1848)', async () => {
    const { onCopy } = renderChip()
    fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy session key' }))
    await waitFor(() => {
      expect(onCopy).toHaveBeenCalledWith(CURRENT)
      expect(toast.info).toHaveBeenCalledWith('Session key copied')
    })
  })

  it('resets the current session via onReset (chat.js:2723)', () => {
    const { onReset } = renderChip()
    fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Reset session' }))
    expect(onReset).toHaveBeenCalledTimes(1)
  })

  it('exports from the compact Chat actions menu', () => {
    const onExport = vi.fn()
    renderChip({ onExport })
    fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Export chat as Markdown' }))
    expect(onExport).toHaveBeenCalledTimes(1)
  })

  it('anchors the actions menu to the actions trigger wrapper', () => {
    renderChip({ onExport: vi.fn() })
    fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }))
    const menu = screen.getByRole('menu', { name: 'Chat actions' })
    expect(menu.parentElement).toHaveClass('chat-session-actions')
    expect(menu.parentElement).toContainElement(
      screen.getByRole('button', { name: 'Chat actions' }),
    )
  })

  it('keeps action-menu focus keyboard-friendly without fetching sessions', async () => {
    const { fetchSessions } = renderChip({ onExport: vi.fn() })
    const trigger = screen.getByRole('button', { name: 'Chat actions' })
    fireEvent.click(trigger)

    const copy = screen.getByRole('menuitem', { name: 'Copy session key' })
    const reset = screen.getByRole('menuitem', { name: 'Reset session' })
    await waitFor(() => expect(copy).toHaveFocus())
    expect(screen.getAllByRole('menuitem').every((item) => item.tabIndex === -1)).toBe(true)
    expect(fetchSessions).not.toHaveBeenCalled()

    fireEvent.keyDown(copy, { key: 'ArrowDown' })
    expect(reset).toHaveFocus()
    fireEvent.keyDown(reset, { key: 'End' })
    expect(screen.getByRole('menuitem', { name: 'Export chat as Markdown' })).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('menu', { name: 'Chat actions' })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())

    fireEvent.click(trigger)
    const reopenedCopy = screen.getByRole('menuitem', { name: 'Copy session key' })
    await waitFor(() => expect(reopenedCopy).toHaveFocus())
    fireEvent.keyDown(reopenedCopy, { key: 'Tab' })
    expect(screen.queryByRole('menu', { name: 'Chat actions' })).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('degrades to manual key entry when the session list fetch fails (chat.js:2038-2069)', async () => {
    const { onSwitch } = renderChip({
      fetchSessions: vi.fn().mockRejectedValue(new Error('offline')),
    })
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    // The manual-entry note + a key field pre-filled to the current key.
    expect(await screen.findByText(/Session list unavailable/i)).toBeInTheDocument()
    const field = screen.getByRole('searchbox', { name: /session key/i }) as HTMLInputElement
    expect(field.value).toBe(CURRENT)
    fireEvent.change(field, { target: { value: 'agent:main:webchat:typed' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onSwitch).toHaveBeenCalledWith('agent:main:webchat:typed')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /switch chat session/i })).toHaveFocus(),
    )
  })

  it('keeps a short visible run-status label for narrow headers', () => {
    renderChip({
      runState: { status: 'approval_pending', label: 'Waiting for approval', task: null },
    })
    expect(document.querySelector('.chat-session-run-status__full')).toHaveTextContent(
      'Waiting for approval',
    )
    expect(document.querySelector('.chat-session-run-status__compact')).toHaveTextContent('Wait')
  })

  it('shows an empty-state when no sessions come back (chat.js:1955)', async () => {
    renderChip({ fetchSessions: vi.fn().mockResolvedValue([]) })
    fireEvent.click(screen.getByRole('button', { name: /switch chat session/i }))
    expect(await screen.findByText('No sessions found.')).toBeInTheDocument()
  })
})
