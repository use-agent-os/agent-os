import '@/test/web'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePendingQuestions } from '@/lib/pending-questions'
import { useApprovals, type Approval } from '@/services/approval-monitor'
import { stubMatchMedia } from '@/test/web'
import { NOW, renderShell, resetShellState, sessionRow, shellRpc } from '@/test/web-shell'
import { useShell } from './shell-store'

const originalMatchMedia = window.matchMedia

beforeEach(resetShellState)
afterEach(() => {
  usePendingQuestions.getState().reset()
  window.matchMedia = originalMatchMedia
  vi.unstubAllGlobals()
})

function phone() {
  stubMatchMedia((q) => q.includes('max-width: 768px') || q.includes('prefers-reduced-motion'))
}

const approval = (extra: Partial<Approval> = {}): Approval => ({
  id: 'ap1',
  namespace: 'exec',
  toolName: 'bash',
  sessionKey: 'agent:main:webchat:a',
  command: 'rm -rf build',
  ...extra,
})

describe('mobile drawer', () => {
  it('opens from the header, closes on navigation and focuses the main area', async () => {
    phone()
    const user = userEvent.setup()
    const { router } = renderShell(
      '/home',
      shellRpc([sessionRow('agent:main:webchat:a', { display_name: 'Phone chat' })]),
    )
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    const drawer = await screen.findByRole('dialog', { name: 'Navigation' })
    await user.click(await within(drawer).findByRole('link', { name: /Phone chat/ }))
    expect(router.state.location.pathname).toBe('/chat/main/webchat/a')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(document.getElementById('main-content')).toHaveFocus())
  })

  it('closes on Escape and keeps New session in the header', async () => {
    phone()
    const user = userEvent.setup()
    renderShell('/home')
    expect(screen.getByRole('button', { name: 'New session' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await screen.findByRole('dialog', { name: 'Navigation' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('opens on a rightward swipe from the left edge', async () => {
    phone()
    renderShell('/home')
    fireEvent.touchStart(document, { touches: [{ clientX: 5, clientY: 300 }] })
    fireEvent.touchMove(document, { touches: [{ clientX: 90, clientY: 305 }] })
    expect(useShell.getState().drawerOpen).toBe(true)
  })

  it('stays closed while the user scrolls vertically', () => {
    phone()
    renderShell('/home')
    fireEvent.touchStart(document, { touches: [{ clientX: 10, clientY: 300 }] })
    fireEvent.touchMove(document, { touches: [{ clientX: 40, clientY: 420 }] })
    fireEvent.touchMove(document, { touches: [{ clientX: 120, clientY: 430 }] })
    expect(useShell.getState().drawerOpen).toBe(false)
  })
})

describe('inbox attention badge', () => {
  it('shows 1 and announces politely when an approval arrives, without moving the rail', async () => {
    renderShell('/home')
    const rail = await screen.findByRole('navigation', { name: 'Main navigation' })
    const before = [...rail.querySelectorAll('button, a')].map((el) => el.className)
    const badge = screen.getByTestId('inbox-badge')
    const baseline = Number(badge.textContent)
    expect(badge).toHaveClass(baseline > 0 ? 'visible' : 'invisible')

    act(() => useApprovals.getState().upsert(approval()))

    await waitFor(() => expect(badge.textContent).toBe(String(baseline + 1)))
    expect(badge).toHaveClass('visible', 'absolute')
    expect(
      screen.getByRole('link', { name: /Inbox, \d+ items need attention/ }),
    ).toBeInTheDocument()
    expect(
      screen
        .getAllByRole('status')
        .some((s) => /items? needs? attention/.test(s.textContent ?? '')),
    ).toBe(true)
    // The badge is absolutely positioned in a reserved slot: no rail control
    // changed its classes (and so its box) when it appeared.
    expect([...rail.querySelectorAll('button, a')].map((el) => el.className)).toEqual(before)
  })
})

describe('pending questions of deleted sessions', () => {
  const question = (sessionKey: string, presentedAt: number) => ({
    id: `question:${sessionKey}`,
    sessionKey,
    questions: [{ question: 'Proceed?', options: [{ label: 'yes' }], multiSelect: false }],
    toolUseId: 't1',
    presentedAt,
  })

  it('drops a question whose session is gone from the list on any page', async () => {
    usePendingQuestions.getState().present(question('agent:main:webchat:gone', NOW - 60_000))
    renderShell('/home', shellRpc([sessionRow('agent:main:webchat:other')]))
    await waitFor(() => expect(usePendingQuestions.getState().items).toEqual([]))
  })

  it('clears a question when its session is deleted from the session list', async () => {
    const user = userEvent.setup()
    const key = 'agent:main:webchat:a'
    // Newer than any list read, so only the delete itself can clear it.
    usePendingQuestions.getState().present(question(key, Date.now() + 60_000))
    const rpc = shellRpc([sessionRow(key, { display_name: 'Asking' })]).handle('sessions.delete', {
      ok: true,
    })
    renderShell('/home', rpc)
    await user.click(await screen.findByRole('button', { name: 'Actions for Asking' }))
    expect(usePendingQuestions.getState().items).toHaveLength(1)
    await user.click(await screen.findByRole('menuitem', { name: 'Delete' }))
    await user.click(await screen.findByRole('button', { name: 'Delete session' }))
    await waitFor(() => expect(usePendingQuestions.getState().items).toEqual([]))
    expect(rpc.callsTo('sessions.delete')).toEqual([{ key }])
  })
})

describe('approval tray', () => {
  it('shows a request from another session on /usage and resolves Deny through the approvals endpoint', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    renderShell('/usage')
    act(() => useApprovals.getState().upsert(approval()))
    const tray = await screen.findByRole('complementary', { name: 'Pending approvals' })
    expect(within(tray).getByText('bash')).toBeInTheDocument()
    expect(within(tray).getByText('From agent:main:webchat:a')).toBeInTheDocument()
    expect(within(tray).getByText('rm -rf build')).toBeInTheDocument()
    for (const name of ['Allow once', 'Always allow', 'Bypass', 'Deny']) {
      expect(within(tray).getByRole('button', { name })).toBeInTheDocument()
    }
    await user.click(within(tray).getByRole('button', { name: 'Deny' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toContain('/api/approvals/resolve')
    expect(JSON.parse(String(init.body))).toMatchObject({ id: 'ap1', approved: false })
    await waitFor(() =>
      expect(
        screen.queryByRole('complementary', { name: 'Pending approvals' }),
      ).not.toBeInTheDocument(),
    )
  })

  it('names the requesting agent and copies the command', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderShell('/usage')
    act(() => useApprovals.getState().upsert(approval({ agent: 'research' })))
    const tray = await screen.findByRole('complementary', { name: 'Pending approvals' })
    expect(within(tray).getByText('Agent research')).toBeInTheDocument()
    await user.click(within(tray).getByRole('button', { name: 'Copy command' }))
    expect(writeText).toHaveBeenCalledWith('rm -rf build')
  })

  it('offers no copy control when the request has no command', async () => {
    renderShell('/usage')
    act(() => useApprovals.getState().upsert(approval({ namespace: 'flow', command: undefined })))
    const tray = await screen.findByRole('complementary', { name: 'Pending approvals' })
    expect(within(tray).queryByRole('button', { name: 'Copy command' })).not.toBeInTheDocument()
    expect(within(tray).queryByText(/^Agent /)).not.toBeInTheDocument()
  })

  it('collapses to a count but cannot be dismissed while pending', async () => {
    const user = userEvent.setup()
    renderShell('/usage')
    act(() => useApprovals.getState().upsert(approval()))
    const tray = await screen.findByRole('complementary', { name: 'Pending approvals' })
    await user.click(within(tray).getByRole('button', { name: 'Collapse approvals' }))
    expect(within(tray).queryByRole('button', { name: 'Deny' })).not.toBeInTheDocument()
    expect(within(tray).getByRole('heading', { name: '1 approval waiting' })).toBeInTheDocument()
    expect(within(tray).queryByRole('button', { name: /close|dismiss/i })).not.toBeInTheDocument()
  })

  it('starts collapsed on a chat page, and a choice made there holds on other pages', async () => {
    const user = userEvent.setup()
    const { router } = renderShell('/chat/main/webchat/b')
    act(() => useApprovals.getState().upsert(approval()))
    const tray = () => screen.findByRole('complementary', { name: 'Pending approvals' })
    expect(
      await within(await tray()).findByRole('heading', { name: '1 approval waiting' }),
    ).toBeInTheDocument()
    expect(within(await tray()).queryByRole('button', { name: 'Deny' })).not.toBeInTheDocument()
    // Untouched, it follows the page: expanded away from chat.
    await act(() => router.navigate('/usage'))
    expect(await within(await tray()).findByRole('button', { name: 'Deny' })).toBeInTheDocument()
    await act(() => router.navigate('/chat/main/webchat/b'))
    await user.click(
      await within(await tray()).findByRole('button', { name: 'Show pending approvals' }),
    )
    expect(await within(await tray()).findByRole('button', { name: 'Deny' })).toBeInTheDocument()
    await act(() => router.navigate('/chat/main/webchat/c'))
    expect(await within(await tray()).findByRole('button', { name: 'Deny' })).toBeInTheDocument()
  })

  it('does not offer Always allow when the request cannot be always-allowed', async () => {
    renderShell('/usage')
    act(() => useApprovals.getState().upsert(approval({ namespace: 'flow', command: undefined })))
    const tray = await screen.findByRole('complementary', { name: 'Pending approvals' })
    expect(within(tray).queryByRole('button', { name: 'Always allow' })).not.toBeInTheDocument()
    expect(within(tray).queryByRole('button', { name: 'Bypass' })).not.toBeInTheDocument()
    expect(within(tray).getByRole('button', { name: 'Allow once' })).toBeInTheDocument()
  })

  it('leaves the open session’s request to the chat dock', async () => {
    renderShell('/chat/main/webchat/a')
    act(() => useApprovals.getState().upsert(approval()))
    await new Promise((r) => setTimeout(r, 10))
    expect(
      screen.queryByRole('complementary', { name: 'Pending approvals' }),
    ).not.toBeInTheDocument()
  })
})
