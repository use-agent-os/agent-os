import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import { Toolbar } from './Toolbar'

vi.mock('@/app/providers', () => ({ useRpc: () => ({ call: vi.fn(async () => ({})) }) }))

beforeEach(() => {
  useConnection.getState().setState('disconnected')
})

describe('Toolbar', () => {
  it('has no Inspector button: there is no right-side panel for it to open', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Toolbar />
      </QueryClientProvider>,
    )
    expect(screen.queryByRole('button', { name: 'Inspector' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Settings' })).toBeInTheDocument()
  })
})
