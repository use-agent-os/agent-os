import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Elsewhere } from './MissionControls'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

const OTHER = 'agent:trading:webchat:trading-other'

function mount(sessionKey: string | null = OTHER) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Elsewhere sessionKey={sessionKey} />
    </QueryClientProvider>,
  )
  return client
}

/** The gateway answered this key; the chip's fate is settled once it has. */
async function answered() {
  await waitFor(() => expect(rpcCall).toHaveBeenCalled())
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

beforeEach(() => {
  rpcCall.mockReset()
})

describe('Elsewhere', () => {
  it('asks the gateway about this one chat, not the capped session list', async () => {
    rpcCall.mockResolvedValue({ previews: [{ key: OTHER }] })
    mount()
    await answered()
    expect(rpcCall).toHaveBeenCalledWith('sessions.preview', { keys: [OTHER] })
    expect(screen.getByTestId('mission-elsewhere')).toHaveAttribute('data-session', OTHER)
  })

  it('shows nothing once the chat that filed the row is deleted', async () => {
    rpcCall.mockResolvedValue({ previews: [] })
    mount()
    await waitFor(() => expect(screen.queryByTestId('mission-elsewhere')).toBeNull())
  })

  it('goes away when the chat is deleted while the row is on screen', async () => {
    rpcCall.mockResolvedValue({ previews: [{ key: OTHER }] })
    const client = mount()
    await answered()
    expect(screen.getByTestId('mission-elsewhere')).toBeInTheDocument()

    rpcCall.mockResolvedValue({ previews: [] })
    // What the sidebar's delete and the `sessions.changed` event both do.
    await act(() => client.invalidateQueries({ queryKey: ['sessions'] }))
    await waitFor(() => expect(screen.queryByTestId('mission-elsewhere')).toBeNull())
  })

  it('keeps the way back until the gateway says the chat is gone', async () => {
    rpcCall.mockReturnValue(new Promise(() => {}))
    mount()
    expect(screen.getByTestId('mission-elsewhere')).toBeInTheDocument()
  })

  it.each([
    ['fails', () => rpcCall.mockRejectedValue(new Error('offline'))],
    ['answers without a list', () => rpcCall.mockResolvedValue({})],
  ])('keeps the way back when the gateway %s', async (_name, arrange) => {
    arrange()
    mount()
    await answered()
    expect(screen.getByTestId('mission-elsewhere')).toBeInTheDocument()
  })

  it('asks nothing for a row of this chat', async () => {
    mount(null)
    await act(async () => {
      await Promise.resolve()
    })
    expect(rpcCall).not.toHaveBeenCalled()
    expect(screen.queryByTestId('mission-elsewhere')).toBeNull()
  })
})
