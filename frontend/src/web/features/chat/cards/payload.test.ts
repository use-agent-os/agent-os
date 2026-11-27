import { afterEach, describe, expect, it, vi } from 'vitest'
import { bindGatewaySocket } from '@/lib/api-origin'
import { FakeArtifactGateway } from '@/test/artifact-gateway'
import { FakeRpc } from '@/test/fake-rpc'
import { fetchArtifactJson } from './payload'

const KEY = 'agent:main:webchat:t1'

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('fetchArtifactJson', () => {
  it('on another gateway than the page origin it says why instead of fetching', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const rpc = new FakeRpc()
    sessionStorage.setItem('agentos.wsToken', 'tokB')
    const unbind = bindGatewaySocket({ currentUrl: 'ws://127.0.0.1:28812/ws' })
    try {
      await expect(fetchArtifactJson(rpc, { id: 'a1', name: 'chart.json' }, KEY)).rejects.toThrow(
        /reaches only the gateway that served this page/,
      )
      expect(fetchMock).not.toHaveBeenCalled()
      expect(rpc.calls).toEqual([])
    } finally {
      unbind()
    }
  })

  it('on the page origin it reads a ticket for the session, with no token', async () => {
    sessionStorage.setItem('agentos.wsToken', 'tokA')
    const rpc = new FakeRpc()
    const gateway = new FakeArtifactGateway(KEY, { token: 'tokA' }).put('a1', { x: 1 }).install(rpc)
    await expect(fetchArtifactJson(rpc, gateway.artifact('a1'), KEY)).resolves.toEqual({ x: 1 })
    expect(rpc.callsTo('artifacts.download')).toEqual([
      { session_key: KEY, artifact_id: 'a1', transport: 'http' },
    ])
    expect(gateway.requests).toEqual(['/api/v1/artifacts/download/conn-1/ticket-1'])
    expect(vi.mocked(fetch).mock.calls[0]![1]).toEqual({ credentials: 'same-origin' })
  })

  it('reads an artifact named only by its URL', async () => {
    const rpc = new FakeRpc()
    const gateway = new FakeArtifactGateway(KEY).put('a2', [1, 2]).install(rpc)
    await expect(
      fetchArtifactJson(rpc, { download_url: '/api/v1/artifacts/a2?sessionKey=x' }, KEY),
    ).resolves.toEqual([1, 2])
    expect(rpc.callsTo('artifacts.download')[0]).toMatchObject({ session_key: KEY })
    expect(gateway.legacyRequests).toEqual([])
  })

  it('mints a fresh ticket once when the cached one stopped working', async () => {
    const rpc = new FakeRpc()
    const gateway = new FakeArtifactGateway(KEY).put('a1', { v: 1 }).install(rpc)
    await fetchArtifactJson(rpc, gateway.artifact('a1'), KEY)
    // Evicted (the per-connection cap) or expired: the cached URL is now a 404.
    gateway.dropTickets()
    gateway.put('a1', { v: 2 })
    await expect(fetchArtifactJson(rpc, gateway.artifact('a1'), KEY)).resolves.toEqual({ v: 2 })
    expect(rpc.callsTo('artifacts.download')).toHaveLength(2)
  })

  it("passes on the gateway's refusal", async () => {
    const rpc = new FakeRpc()
    new FakeArtifactGateway(KEY).install(rpc)
    await expect(fetchArtifactJson(rpc, { id: 'missing' }, KEY)).rejects.toThrow(
      'artifact not found',
    )
  })

  it('says so when this connection has no tickets', async () => {
    const rpc = new FakeRpc()
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    // The socket is down: the call fails without a gateway error code.
    rpc.handle('artifacts.download', () => {
      throw new Error('not connected')
    })
    await expect(fetchArtifactJson(rpc, { id: 'a1' }, KEY)).rejects.toThrow(/gave no download link/)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
