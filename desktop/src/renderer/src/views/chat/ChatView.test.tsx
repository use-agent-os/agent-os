import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { useGateway } from '~/stores/gateway'
import { ChatView } from './ChatView'

// The shared chat hooks run for real, against this stand-in for the gateway.
const KEY = 'agent:main:webchat:header-test'
let answers: Record<string, unknown> = {}
const rpc = {
  waitForConnection: vi.fn(async () => {}),
  call: vi.fn(async (method: string) => answers[method] ?? {}),
  on: vi.fn(() => () => {}),
}
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))
// The console's route picker imports its own copy of lucide-react, which the
// test build does not dedupe the way the app build does. It plays no part here.
vi.mock('@/views/chat/RoutePicker', () => ({ RoutePicker: () => null }))

let slot: HTMLDivElement

function mount(actionsSlot: HTMLElement | null | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[`/sessions/${encodeURIComponent(KEY)}`]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/sessions/:key?" element={<ChatView actionsSlot={actionsSlot} />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const controls = (root: HTMLElement) =>
  within(root).getByRole('group', { name: 'Chat session controls' })

beforeEach(() => {
  answers = { 'sessions.list': { sessions: [{ key: KEY }] }, 'projects.list': { projects: [] } }
  rpc.call.mockClear()
  useConnection.getState().setState('connected')
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
  // Stands in for the mode strip's slot, which lives outside the chat's tree.
  slot = document.createElement('div')
  document.body.appendChild(slot)
})

afterEach(() => {
  slot.remove()
})

describe('ChatView header', () => {
  it('puts the session actions on the strip, with no title row above the chat', async () => {
    const { container } = mount(slot)
    const group = controls(slot)
    for (const name of ['New chat', 'Reset session', 'Export as Markdown']) {
      expect(within(group).getByRole('button', { name })).toBeInTheDocument()
    }
    // The project chip comes along once the gateway lists the session.
    const chip = await within(group).findByRole('button', { name: 'Project' })
    expect(chip).toHaveAttribute('title', 'Add to project')
    expect(container.querySelector('.chat-desktop-header')).toBeNull()
    // The sidebar names the chat: the view keeps only its landmark heading.
    expect(screen.getAllByRole('heading').map((h) => h.textContent)).toEqual(['Chat'])
  })

  it('keeps every action working from the strip', async () => {
    mount(slot)
    await act(async () => {
      fireEvent.click(within(controls(slot)).getByRole('button', { name: 'Reset session' }))
    })
    expect(rpc.call).toHaveBeenCalledWith('sessions.reset', { key: KEY })
    // New chat leaves for the keyless home, where there is nothing to act on yet.
    fireEvent.click(within(controls(slot)).getByRole('button', { name: 'New chat' }))
    await waitFor(() => expect(within(slot).queryByRole('group')).toBeNull())
  })

  it("hangs the project menu from the chip's right edge, into the pane", async () => {
    mount(slot)
    fireEvent.click(await within(controls(slot)).findByRole('button', { name: 'Project' }))
    // The chip sits at the right of the strip with only the buttons after it:
    // a 200 px menu grown rightwards from a short chip (align start) ran past
    // the window's edge and scrolled the pane sideways.
    expect(within(slot).getByRole('menu', { name: 'Project' })).toHaveAttribute('data-align', 'end')
  })

  it('keeps a row of its own when the route gives no slot (the desk)', () => {
    const { container } = mount(undefined)
    const row = container.querySelector<HTMLElement>('.chat-desktop-header')
    expect(row).not.toBeNull()
    expect(controls(row as HTMLElement)).toBeInTheDocument()
    expect(slot).toBeEmptyDOMElement()
    expect(screen.getAllByRole('heading').map((h) => h.textContent)).toEqual(['Chat'])
  })

  it('shows nothing until the strip has mounted its slot, rather than flash a row', () => {
    const { container } = mount(null)
    expect(screen.queryByRole('group', { name: 'Chat session controls' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset session' })).toBeNull()
    expect(container.querySelector('.chat-desktop-header')).toBeNull()
  })

  it('keeps the run state as a word the strip can fold into its dot, with a tooltip', async () => {
    answers['sessions.messages.subscribe'] = { subscribed: true, run_status: 'failed' }
    mount(slot)
    const word = await within(controls(slot)).findByText('Failed')
    // chat.css hides this element, not the text node, on a narrow strip.
    expect(word).toHaveClass('chat-desktop-actions__state-text')
    expect(word.parentElement).toHaveClass('chat-desktop-actions__state')
    expect(word.parentElement).toHaveAttribute('title', 'Failed')
    expect(word.parentElement).toHaveAttribute('data-tone', 'danger')
  })
})
