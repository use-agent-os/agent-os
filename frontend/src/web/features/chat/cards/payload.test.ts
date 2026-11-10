import { afterEach, describe, expect, it, vi } from 'vitest'
import { bindGatewaySocket } from '@/lib/api-origin'
import { fetchArtifactJson } from './payload'

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('fetchArtifactJson', () => {
  it('on another gateway than the page origin it says why instead of fetching', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    sessionStorage.setItem('agentos.wsToken', 'tokB')
    const unbind = bindGatewaySocket({ currentUrl: 'ws://127.0.0.1:28812/ws' })
    try {
      await expect(fetchArtifactJson({ id: 'a1', name: 'chart.json' })).rejects.toThrow(
        /reaches only the gateway that served this page/,
      )
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      unbind()
    }
  })

  it('on the page origin it fetches with the token', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ x: 1 }) })
    vi.stubGlobal('fetch', fetchMock)
    sessionStorage.setItem('agentos.wsToken', 'tokA')
    await expect(fetchArtifactJson({ id: 'a1' })).resolves.toEqual({ x: 1 })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/artifacts/a1',
      expect.objectContaining({ headers: { Authorization: 'Bearer tokA' } }),
    )
  })
})
