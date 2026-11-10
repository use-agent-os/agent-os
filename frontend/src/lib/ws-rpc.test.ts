import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpcError, WsRpcClient, callIdempotent } from './ws-rpc'

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
    expect(req.params.client).toEqual({
      id: 'agentos-web',
      version: 'bundled',
      platform: 'web',
      mode: 'webui',
    })
    expect(req.params.auth).toEqual({ token: 'tok-1' })
  })

  it('merges extra auth (the desktop operator secret) into the handshake', () => {
    const client = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    client.connect('ws://test/ws', 'tok-1', { operatorSecret: 'op-secret' })
    const ws = FakeWebSocket.instances.at(-1)!
    ws.serverOpen()
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const req = JSON.parse(ws.sent.at(-1)!)
    expect(req.params.auth).toEqual({ token: 'tok-1', operatorSecret: 'op-secret' })
    // Without a token the extra auth still goes; with nothing at all, no auth key.
    const bare = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    bare.connect('ws://test/ws', null, {})
    const ws2 = FakeWebSocket.instances.at(-1)!
    ws2.serverOpen()
    ws2.serverSend({ type: 'event', event: 'connect.challenge' })
    expect(JSON.parse(ws2.sent.at(-1)!).params.auth).toBeUndefined()
  })

  it('sends a caller-supplied client block, updatable before the next handshake', () => {
    const client = new WsRpcClient({
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
      clientInfo: { id: 'agentos-desktop', platform: 'darwin', mode: 'desktop' },
    })
    client.setClientInfo({ version: '2026.10.25' })
    client.connect('ws://test/ws', null)
    const ws = FakeWebSocket.instances.at(-1)!
    ws.serverOpen()
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n1', ts: 1 } })
    expect(JSON.parse(ws.sent.at(-1)!).params.client).toEqual({
      id: 'agentos-desktop',
      version: '2026.10.25',
      platform: 'darwin',
      mode: 'desktop',
    })
  })

  it('exposes hello-ok capabilities', () => {
    const { client, ws } = newClient()
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n1' } })
    ws.serverSend({
      protocol: 3,
      auth: { nonce: 'n1' },
      features: { methods: [], events: [], capabilities: ['rpc-http', 'typed-errors'] },
      policy: {},
    })
    expect(client.state).toBe('connected')
    expect(client.capabilities).toEqual(['rpc-http', 'typed-errors'])
    expect(client.hasCapability('typed-errors')).toBe(true)
    expect(client.hasCapability('nope')).toBe(false)
  })

  it('refuses a hello-ok that echoes another challenge nonce', () => {
    const { client, ws } = newClient()
    const errors: unknown[] = []
    client.on('_handshakeError', (e) => errors.push(e))
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'mine' } })
    ws.serverSend({ protocol: 3, auth: { nonce: 'theirs' }, policy: {} })
    expect(client.state).not.toBe('connected')
    expect(errors).toHaveLength(1)
  })

  it('stops on a refused handshake and surfaces the typed error', () => {
    const { client, ws } = newClient()
    const errors: unknown[] = []
    client.on('_handshakeError', (e) => errors.push(e))
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n' } })
    const req = JSON.parse(ws.sent.at(-1)!)
    ws.serverSend({
      type: 'res',
      id: req.id,
      ok: false,
      error: {
        code: 'INVALID_REQUEST',
        message: 'Unsupported protocol version range',
        details: { code: 'PROTOCOL_MISMATCH', reload_required: true },
      },
    })
    expect(client.state).toBe('disconnected')
    expect(errors).toHaveLength(1)
    expect(errors[0]).toMatchObject({
      code: 'INVALID_REQUEST',
      details: { code: 'PROTOCOL_MISMATCH', reload_required: true },
    })
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1) // no reconnect loop
  })

  it('reconnects when the socket drops mid-handshake (not a refusal)', () => {
    const { client, ws } = newClient()
    const errors: unknown[] = []
    client.on('_handshakeError', (e) => errors.push(e))
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n' } })
    ws.close()
    expect(errors).toHaveLength(0)
    vi.advanceTimersByTime(800)
    expect(FakeWebSocket.instances).toHaveLength(2)
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

  it('drops the socket when no frame arrives within the tick timeout', () => {
    const { client, ws } = newClient()
    handshake(ws) // tick_interval_ms 30000 -> timeout 75s, checked every 10s
    const gap = vi.fn()
    client.on('_gap', gap)
    // A silent peer never completes the close handshake: the client must not
    // wait for it before calling the connection lost.
    ws.close = () => {}
    const count = FakeWebSocket.instances.length
    // Idle exceeds the 75s timeout at the next 10s check tick (80s).
    vi.advanceTimersByTime(80_000)
    expect(gap).toHaveBeenCalledWith(expect.objectContaining({ reason: 'tick_timeout' }))
    expect(client.state).toBe('disconnected')
    expect(FakeWebSocket.instances.length).toBe(count)
    vi.advanceTimersByTime(800)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })
})

describe('supervisor contract', () => {
  function refuseStartup(ws: FakeWebSocket, retryAfterMs = 1000) {
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const req = JSON.parse(ws.sent.at(-1)!)
    ws.serverSend({
      type: 'res',
      id: req.id,
      ok: false,
      error: {
        code: 'UNAVAILABLE',
        message: 'Gateway is starting; retry shortly',
        retryable: true,
        details: { code: 'STARTUP_SIDECARS', retry_after_ms: retryAfterMs },
      },
    })
  }

  it('retries a handshake refused while the gateway starts, staying connecting', () => {
    const { client, ws } = newClient()
    const states: unknown[] = []
    client.on('_state', (s) => states.push(s))
    const count = FakeWebSocket.instances.length
    refuseStartup(ws, 1200)
    expect(client.state).toBe('connecting')
    expect(states).not.toContain('disconnected')
    vi.advanceTimersByTime(1199)
    expect(FakeWebSocket.instances.length).toBe(count)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
    const next = FakeWebSocket.instances.at(-1)!
    next.serverOpen()
    handshake(next)
    expect(client.state).toBe('connected')
  })

  it('reconnects when an in-place restart said it would be back', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const count = FakeWebSocket.instances.length
    ws.serverSend({
      type: 'event',
      event: 'shutdown',
      payload: { reason: 'restart:safe', restart_expected_ms: 4000 },
    })
    ws.close()
    expect(client.state).toBe('disconnected')
    vi.advanceTimersByTime(3999)
    expect(FakeWebSocket.instances.length).toBe(count)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })

  it('keeps the normal backoff after a terminal stop', () => {
    const { ws } = newClient()
    handshake(ws)
    const count = FakeWebSocket.instances.length
    ws.serverSend({
      type: 'event',
      event: 'shutdown',
      payload: { reason: 'shutdown', restart_expected_ms: null },
    })
    ws.close()
    vi.advanceTimersByTime(800)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })
})

describe('callIdempotent', () => {
  it('retries a transport failure with the same idempotency key once reconnected', async () => {
    const sent: Record<string, unknown>[] = []
    let failures = 1
    const rpc = {
      call: vi.fn(async (_method: string, params?: Record<string, unknown>) => {
        sent.push(params ?? {})
        if (failures > 0) {
          failures -= 1
          throw new Error('Connection closed')
        }
        return { task_id: 't1' }
      }),
      waitForConnection: vi.fn().mockResolvedValue(undefined),
    }

    const result = await callIdempotent(rpc as never, 'chat.send', { message: 'hi' })

    expect(result).toEqual({ task_id: 't1' })
    expect(sent).toHaveLength(2)
    expect(typeof sent[0]!.idempotency_key).toBe('string')
    expect(sent[1]!.idempotency_key).toBe(sent[0]!.idempotency_key)
    expect(sent[1]!.message).toBe('hi')
  })

  it('never retries an answer from the gateway', async () => {
    const rpcError = new RpcError('queue full')
    rpcError.code = 'QUEUE_FULL'
    const rpc = {
      call: vi.fn().mockRejectedValue(rpcError),
      waitForConnection: vi.fn().mockResolvedValue(undefined),
    }

    await expect(callIdempotent(rpc as never, 'chat.send', {})).rejects.toBe(rpcError)
    expect(rpc.call).toHaveBeenCalledTimes(1)
  })

  it('keeps a caller-provided key and gives up when the socket does not come back', async () => {
    vi.useFakeTimers()
    try {
      const rpc = {
        call: vi.fn().mockRejectedValue(new Error('Not connected')),
        waitForConnection: vi.fn(() => new Promise<void>(() => {})),
      }
      const pending = callIdempotent(
        rpc as never,
        'chat.send',
        { idempotency_key: 'mine' },
        { reconnectTimeoutMs: 1000 },
      )
      const settled = expect(pending).rejects.toThrow('Not connected')
      await vi.advanceTimersByTimeAsync(1500)
      await settled
      expect(rpc.call).toHaveBeenCalledTimes(1)
      expect(rpc.call.mock.calls[0]![1]).toEqual({ idempotency_key: 'mine' })
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('handshake refusal and manual retry', () => {
  function refuse(ws: FakeWebSocket, code: string, message: string) {
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const req = JSON.parse(ws.sent.at(-1)!)
    ws.serverSend({ type: 'res', id: req.id, ok: false, error: { code, message } })
  }

  it('emits the typed refusal and does not retry by itself', () => {
    const { client, ws } = newClient()
    const refused = vi.fn()
    client.on('_handshakeError', refused)
    refuse(ws, 'UNAUTHORIZED', 'Authentication failed')
    expect(refused).toHaveBeenCalledWith(
      expect.objectContaining({ code: 'UNAUTHORIZED', message: 'Authentication failed' }),
    )
    expect(client.state).toBe('disconnected')
    const count = FakeWebSocket.instances.length
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances.length).toBe(count)
  })

  it('reconnect() opens a new socket at once after a refusal', () => {
    const { client, ws } = newClient()
    refuse(ws, 'UNAUTHORIZED', 'Authentication failed')
    const count = FakeWebSocket.instances.length
    client.reconnect()
    expect(FakeWebSocket.instances.length).toBe(count + 1)
    expect(client.state).toBe('connecting')
    const next = FakeWebSocket.instances.at(-1)!
    next.serverOpen()
    handshake(next)
    expect(client.state).toBe('connected')
  })

  it('reconnect() skips the backoff wait and leaves a live connection alone', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const live = FakeWebSocket.instances.length
    client.reconnect()
    expect(FakeWebSocket.instances.length).toBe(live)
    expect(client.state).toBe('connected')
    ws.close()
    client.reconnect()
    expect(FakeWebSocket.instances.length).toBe(live + 1)
  })

  it('reconnect({force: true}) drops a silent "connected" socket and opens a new one', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const states: unknown[] = []
    client.on('_state', (s) => states.push(s))
    const inflight = client.call('a.b')
    // The old socket never answers its close handshake (a frozen peer).
    ws.close = () => {}
    const count = FakeWebSocket.instances.length
    client.reconnect({ force: true })
    await expect(inflight).rejects.toThrow('Connection closed')
    expect(FakeWebSocket.instances.length).toBe(count + 1)
    // It cycles through "disconnected", so subscribers resubscribe on connect.
    expect(states).toEqual(['disconnected', 'connecting'])
    const next = FakeWebSocket.instances.at(-1)!
    next.serverOpen()
    handshake(next)
    expect(client.state).toBe('connected')
    // A late frame on the abandoned socket is ignored.
    expect(ws.onmessage).toBeNull()
  })

  it('a socket that drops mid-handshake still schedules the reconnect', () => {
    const { client, ws } = newClient()
    const refused = vi.fn()
    client.on('_handshakeError', refused)
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const count = FakeWebSocket.instances.length
    ws.close()
    expect(refused).not.toHaveBeenCalled()
    expect(client.state).toBe('disconnected')
    vi.advanceTimersByTime(800)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })

  it('surfaces an event frame state_version as _state_version', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const revision = vi.fn()
    client.on('_state_version', revision)
    ws.serverSend({
      type: 'event',
      event: 'tick',
      payload: {},
      seq: 1,
      state_version: { presence: 1, health: 4 },
    })
    expect(revision).toHaveBeenCalledWith({ presence: 1, health: 4 })
    expect(client.lastFrameTime).toBeGreaterThan(0)
  })
})

describe('connect() while a connection exists', () => {
  it('drops a live socket before opening one to the new URL', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const events = vi.fn()
    client.on('e', events)
    client.connect('ws://other/ws', 'tok-2')
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(ws.readyState).toBe(3)
    const next = FakeWebSocket.instances.at(-1)!
    expect(next.url).toBe('ws://other/ws')
    next.serverOpen()
    handshake(next)
    expect(client.state).toBe('connected')
    // The old socket's late close and frames reach nothing.
    ws.onclose?.()
    ws.onmessage?.({ data: JSON.stringify({ type: 'event', event: 'e', payload: {}, seq: 9 }) })
    expect(events).not.toHaveBeenCalled()
    expect(client.state).toBe('connected')
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    // The live socket still carries events in order.
    next.serverSend({ type: 'event', event: 'e', payload: {}, seq: 1 })
    expect(events).toHaveBeenCalledTimes(1)
  })

  it('cancels a pending backoff timer so only one socket opens', () => {
    const { client, ws } = newClient()
    handshake(ws)
    ws.close() // backoff timer pending (800 ms)
    expect(FakeWebSocket.instances).toHaveLength(1)
    client.connect('ws://test/ws', 'tok-2')
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    const next = FakeWebSocket.instances.at(-1)!
    next.serverOpen()
    next.serverSend({ type: 'event', event: 'connect.challenge' })
    expect(JSON.parse(next.sent.at(-1)!).params.auth).toEqual({ token: 'tok-2' })
  })

  it('a socket replaced by connect() can never clear or replace the current one', () => {
    const { client, ws } = newClient()
    // Keep the old handlers reachable, as a browser would deliver a queued close.
    const lateClose = ws.onclose
    client.connect('ws://other/ws')
    const next = FakeWebSocket.instances.at(-1)!
    next.serverOpen()
    handshake(next)
    lateClose?.()
    expect(client.state).toBe('connected')
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('disconnect() settles in-flight calls and stops reconnecting', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const inflight = client.call('a.b')
    client.disconnect()
    await expect(inflight).rejects.toThrow('Connection closed')
    expect(client.state).toBe('disconnected')
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
