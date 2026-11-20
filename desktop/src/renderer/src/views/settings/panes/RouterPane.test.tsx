import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { RouterPane } from './RouterPane'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

const SNAPSHOT = {
  revision: 'r1',
  status: { hasConfig: true },
  config: { llm: { provider: 'surplus', model: 'luna' }, agentos_router: { strategy: 'pilot-v1' } },
  catalog: {
    providers: [
      {
        providerId: 'surplus',
        label: 'Surplus Intelligence',
        runtimeSupported: true,
        routerSupported: true,
        requiresApiKey: true,
        fields: [],
      },
    ],
    routerProfiles: {
      defaultTier: 'c1',
      profiles: [
        {
          providerId: 'surplus',
          tiers: {
            c0: { model: 'fast-1', description: 'Quick replies' },
            c1: { model: 'mid-1', description: 'Everyday work' },
          },
        },
      ],
    },
  },
}

function renderPane() {
  useConnection.setState({ state: 'connected' })
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <div role="dialog" tabIndex={-1}>
        <RouterPane />
      </div>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'config.snapshot') return SNAPSHOT
    if (method === 'models.list') return [{ id: 'fast-1' }, { id: 'mid-1' }]
    if (method === 'onboarding.router.configure') return { restartRequired: false }
    return {}
  })
})

describe('RouterPane · one save bar for the page', () => {
  it('shows nothing to save until something changes, then names what changed', async () => {
    renderPane()
    const judge = await screen.findByRole('radio', { name: /^LLM judge/ })
    expect(screen.queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
    // The Tiers card no longer carries its own Save: there is one, in the bar.
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()

    fireEvent.click(judge)
    const bar = screen.getByRole('region', { name: 'Unsaved changes' })
    expect(within(bar).getByTestId('router-unsaved')).toHaveTextContent('Routing')

    fireEvent.click(screen.getByRole('radio', { name: 'Make default' }))
    expect(within(bar).getByTestId('router-unsaved')).toHaveTextContent('Routing · Tiers')
  })

  it('reverts every card at once and keeps focus in the sheet', async () => {
    renderPane()
    fireEvent.click(await screen.findByRole('radio', { name: /^Off/ }))
    const bar = screen.getByRole('region', { name: 'Unsaved changes' })
    const revert = within(bar).getByRole('button', { name: 'Revert' })
    revert.focus()
    fireEvent.click(revert)
    expect(screen.queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
    expect(screen.getByRole('radio', { name: /^Pilot/ })).toHaveAttribute('aria-checked', 'true')
    await waitFor(() => expect(screen.getByRole('dialog')).toHaveFocus())
  })

  it('saves the whole page with the revision from the bar', async () => {
    renderPane()
    fireEvent.click(await screen.findByRole('radio', { name: /^Off/ }))
    const bar = screen.getByRole('region', { name: 'Unsaved changes' })
    fireEvent.click(within(bar).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith(
        'onboarding.router.configure',
        expect.objectContaining({ expectedRevision: 'r1' }),
      ),
    )
  })
})
