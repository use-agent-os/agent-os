import '@/test/web'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { FakeRpc } from '@/test/fake-rpc'
import { testQueryClient, WebTestProviders } from '@/test/render-web'
import { useUnpinnedRoute } from './capabilities'

function unpinnedLabel(config: Record<string, unknown>) {
  const rpc = new FakeRpc().handle('config.get', config)
  const queryClient = testQueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <WebTestProviders rpc={rpc} queryClient={queryClient}>
      {children}
    </WebTestProviders>
  )
  return renderHook(() => useUnpinnedRoute(), { wrapper })
}

describe('useUnpinnedRoute', () => {
  it('names the Pilot Router when it is on', async () => {
    const { result } = unpinnedLabel({
      agentos_router: { enabled: true },
      llm: { model: 'base-model' },
    })
    await waitFor(() => expect(result.current).toBe('Auto (Pilot Router)'))
  })

  it('names the configured model when the router is off', async () => {
    const { result } = unpinnedLabel({
      agentos_router: { enabled: false },
      llm: { model: 'base-model' },
    })
    await waitFor(() => expect(result.current).toBe('Default · base-model'))
  })

  it('says "Default model" when the router is off and no model is set', async () => {
    const { result } = unpinnedLabel({ agentos_router: { enabled: false } })
    await waitFor(() => expect(result.current).toBe('Default model'))
  })
})
