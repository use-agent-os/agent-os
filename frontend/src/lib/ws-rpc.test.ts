import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WsRpcClient } from './ws-rpc'

/** Minimal scripted fake for the browser WebSocket API. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static OPEN = 1
  static CONNECTING = 0
  readyState = FakeWebSocket.CONNECTING
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {
    this.readyState = 3
    this.onclose?.()
  }
  // test helpers
  serverOpen() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.()
  }
  serverSend(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
}

function newClient() {
  const client = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
  client.connect('ws://test/ws', 'tok-1')
  const ws = FakeWebSocket.instances.at(-1)!
  ws.serverOpen()
  return { client, ws }
}

function handshake(ws: FakeWebSocket) {
  ws.serverSend({ type: 'event', event: 'connect.challenge' })
  ws.serverSend({ protocol: 3, policy: { tick_interval_ms: 30000 } })
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeWebSocket.instances = []
})
afterEach(() => vi.useRealTimers())

describe('handshake', () => {
  it('answers connect.challenge with a protocol-3 connect request incl. auth token', () => {
    const { ws } = newClient()
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const req = JSON.parse(ws.sent.at(-1)!)
    expect(req.method).toBe('connect')
    expect(req.params.minProtocol).toBe(3)
    expect(req.params.maxProtocol).toBe(3)
    expect(req.params.client).toEqual({ name: 'agentos-web' })
    expect(req.params.auth).toEqual({ token: 'tok-1' })
  })

  it('enters connected state and stores policy on HelloOk', () => {
    const { client, ws } = newClient()
    handshake(ws)
    expect(client.state).toBe('connected')
    expect(client.policy).toEqual({ tick_interval_ms: 30000 })
  })
})

describe('call correlation', () => {
  it('resolves with payload on ok res, matching by id', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const p = client.call<{ n: number }>('doctor.status', { deep: true })
    const req = JSON.parse(ws.sent.at(-1)!)
    expect(req).toMatchObject({ type: 'req', method: 'doctor.status', params: { deep: true } })
    ws.serverSend({ type: 'res', id: req.id, ok: true, payload: { n: 7 } })
    await expect(p).resolves.toEqual({ n: 7 })
  })

  it('rejects with RpcError carrying code and details', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const p = client.call('x.y')
    const req = JSON.parse(ws.sent.at(-1)!)
    ws.serverSend({
      type: 'res',
      id: req.id,
      ok: false,
      error: { code: 'FORBIDDEN', message: 'no', details: { k: 1 } },
    })
    await expect(p).rejects.toMatchObject({ message: 'no', code: 'FORBIDDEN', details: { k: 1 } })
  })

  it('rejects immediately when not connected', async () => {
    const client = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    await expect(client.call('a.b')).rejects.toThrow('Not connected')
  })

  it('rejects all pending calls when the socket closes', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const p = client.call('a.b')
    ws.close()
    await expect(p).rejects.toThrow('Connection closed')
  })
})

describe('events', () => {
  it('fans out to named and wildcard listeners with meta', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const named = vi.fn()
    const wild = vi.fn()
    client.on('sessions.changed', named)
    client.on('*', wild)
    ws.serverSend({
      type: 'event',
      event: 'sessions.changed',
      payload: { a: 1 },
      meta: { m: 2 },
      seq: 1,
    })
    expect(named).toHaveBeenCalledWith({ a: 1 }, { m: 2 })
    expect(wild).toHaveBeenCalledWith('sessions.changed', { a: 1 }, { m: 2 })
  })

  it('detects a seq gap, emits _gap, and closes the socket', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const gap = vi.fn()
    const named = vi.fn()
    client.on('_gap', gap)
    client.on('e', named)
    ws.serverSend({ type: 'event', event: 'e', payload: {}, seq: 1 })
    ws.serverSend({ type: 'event', event: 'e', payload: {}, seq: 3 })
    expect(gap).toHaveBeenCalledWith({ expected: 2, actual: 3, event: 'e' })
    expect(named).toHaveBeenCalledTimes(1) // gapped frame not delivered
  })
})

describe('keepalive and reconnect', () => {
  it('sends a ping every 55s while open', () => {
    const { ws } = newClient()
    handshake(ws)
    vi.advanceTimersByTime(55_000)
    expect(ws.sent).toContain('{"type":"ping"}')
  })

  it('reconnects with backoff after close (800ms first retry)', () => {
    const { ws } = newClient()
    handshake(ws)
    const count = FakeWebSocket.instances.length
    ws.close()
    vi.advanceTimersByTime(799)
    expect(FakeWebSocket.instances.length).toBe(count)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })

  it('closes the socket when no frame arrives within the tick timeout', () => {
    const { client, ws } = newClient()
    handshake(ws) // tick_interval_ms 30000 -> timeout 75s, checked every 10s
    const gap = vi.fn()
    client.on('_gap', gap)
    // Idle exceeds the 75s timeout at the next 10s check tick (80s), so advance past it.
    vi.advanceTimersByTime(81_000)
    expect(gap).toHaveBeenCalledWith(expect.objectContaining({ reason: 'tick_timeout' }))
  })
})
