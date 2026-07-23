/** AgentOS Control — WebSocket RPC client. Typed port of legacy static/js/rpc.js. */

export type RpcState = 'disconnected' | 'connecting' | 'connected'

export class RpcError extends Error {
  code?: string
  details?: unknown
}

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void }
type Handler = (...args: unknown[]) => void

export class WsRpcClient {
  private ws: WebSocket | null = null
  private reqId = 0
  private pending = new Map<string, Pending>()
  private listeners = new Map<string, Set<Handler>>()
  private stateValue: RpcState = 'disconnected'
  private url = ''
  private token: string | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectDelay = 800
  private readonly maxReconnectDelay = 15000
  private readonly reconnectFactor = 1.7
  private autoReconnect = true
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private readonly pingInterval = 55000 // safely under server's 120s keepalive
  private policyValue: Record<string, unknown> | null = null
  private lastSeq = 0
  private lastFrameAt = 0
  private tickWatchTimer: ReturnType<typeof setInterval> | null = null
  private tickTimeoutMs = 60000
  private readonly WebSocketImpl: typeof WebSocket

  constructor(opts?: { WebSocketImpl?: typeof WebSocket }) {
    this.WebSocketImpl = opts?.WebSocketImpl ?? WebSocket
  }

  connect(url: string, token?: string | null): void {
    this.url = url
    this.token = token ?? null
    this.autoReconnect = true
    this.doConnect()
  }

  disconnect(): void {
    this.autoReconnect = false
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.stopPing()
    this.stopTickWatch()
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
    this.setState('disconnected')
  }

  call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== this.WebSocketImpl.OPEN) {
        reject(new Error('Not connected'))
        return
      }
      const id = String(++this.reqId)
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject })
      this.ws.send(JSON.stringify({ type: 'req', id, method, params }))
    })
  }

  on(event: string, handler: Handler): () => void {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set())
    this.listeners.get(event)!.add(handler)
    return () => this.listeners.get(event)?.delete(handler)
  }

  get state(): RpcState {
    return this.stateValue
  }

  get policy(): Record<string, unknown> {
    return this.policyValue ?? {}
  }

  waitForConnection(): Promise<void> {
    if (this.stateValue === 'connected') return Promise.resolve()
    return new Promise((resolve) => {
      const unsub = this.on('_state', (s) => {
        if (s === 'connected') {
          unsub()
          resolve()
        }
      })
    })
  }

  private doConnect(): void {
    this.setState('connecting')
    this.lastSeq = 0
    this.lastFrameAt = Date.now()
    this.stopTickWatch()
    try {
      this.ws = new this.WebSocketImpl(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this.reconnectDelay = 800
      // Don't send connect yet — wait for connect.challenge from server
    }

    this.ws.onmessage = (ev: MessageEvent) => {
      let data: Record<string, unknown>
      try {
        data = JSON.parse(String(ev.data)) as Record<string, unknown>
      } catch {
        return
      }
      if (!this.noteIncomingFrame(data)) return

      // Handshake: server sends connect.challenge, we reply with connect request
      if (data.type === 'event' && data.event === 'connect.challenge') {
        const authParams = this.token ? { auth: { token: this.token } } : {}
        const id = String(++this.reqId)
        this.pending.set(id, {
          resolve: () => {}, // HelloOk is not a res frame, handled below
          reject: () => {
            this.ws?.close()
            this.setState('disconnected')
          },
        })
        this.ws?.send(
          JSON.stringify({
            type: 'req',
            id,
            method: 'connect',
            params: {
              minProtocol: 3,
              maxProtocol: 3,
              client: { name: 'agentos-web' },
              ...authParams,
            },
          }),
        )
        return
      }

      // Handshake: HelloOk frame (has "protocol" field, no "type":"res")
      if (data.protocol !== undefined && this.stateValue === 'connecting') {
        this.policyValue = (data.policy as Record<string, unknown>) ?? null
        for (const [id, p] of this.pending) {
          this.pending.delete(id)
          p.resolve(data)
          break
        }
        this.setState('connected')
        this.emit('_hello', data)
        this.startPing()
        this.startTickWatch()
        return
      }

      if (data.type === 'res') {
        const p = this.pending.get(String(data.id))
        if (p) {
          this.pending.delete(String(data.id))
          if (data.ok) {
            p.resolve(data.payload)
          } else {
            const err = data.error as
              { message?: string; code?: string; details?: unknown } | string
            const message =
              typeof err === 'string' ? err : (err && (err.message || err.code)) || 'RPC error'
            const error = new RpcError(message)
            if (err && typeof err === 'object') {
              error.code = err.code
              error.details = err.details
            }
            p.reject(error)
          }
        }
      } else if (data.type === 'event') {
        const meta = (data.meta as Record<string, unknown>) ?? {}
        this.emit(String(data.event), data.payload, meta)
        this.emit('*', String(data.event), data.payload, meta)
      }
    }

    this.ws.onclose = () => {
      this.stopPing()
      this.stopTickWatch()
      for (const [, p] of this.pending) p.reject(new Error('Connection closed'))
      this.pending.clear()
      this.ws = null
      if (this.stateValue !== 'disconnected') {
        this.setState('disconnected')
        this.scheduleReconnect()
      }
    }

    this.ws.onerror = () => {}
  }

  private emit(event: string, ...args: unknown[]): void {
    this.listeners.get(event)?.forEach((h) => h(...args))
  }

  private startPing(): void {
    this.stopPing()
    this.pingTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === this.WebSocketImpl.OPEN) {
        this.ws.send('{"type":"ping"}')
      }
    }, this.pingInterval)
  }

  private stopPing(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  private noteIncomingFrame(data: Record<string, unknown>): boolean {
    this.lastFrameAt = Date.now()
    if (!data || data.type !== 'event' || typeof data.seq !== 'number') return true
    const seq = data.seq
    if (this.lastSeq > 0 && seq !== this.lastSeq + 1) {
      this.emit('_gap', { expected: this.lastSeq + 1, actual: seq, event: data.event })
      try {
        this.ws?.close()
      } catch {
        /* noop */
      }
      return false
    }
    this.lastSeq = seq
    return true
  }

  private startTickWatch(): void {
    this.stopTickWatch()
    const tickMs = (this.policyValue?.tick_interval_ms as number | undefined) ?? 30000
    this.tickTimeoutMs = Math.max(10000, tickMs * 2.5)
    this.lastFrameAt = Date.now()
    this.tickWatchTimer = setInterval(
      () => {
        if (!this.ws || this.ws.readyState !== this.WebSocketImpl.OPEN) return
        const idleMs = Date.now() - this.lastFrameAt
        if (idleMs <= this.tickTimeoutMs) return
        this.emit('_gap', { reason: 'tick_timeout', idleMs })
        try {
          this.ws.close()
        } catch {
          /* noop */
        }
      },
      Math.min(tickMs, 10000),
    )
  }

  private stopTickWatch(): void {
    if (this.tickWatchTimer !== null) {
      clearInterval(this.tickWatchTimer)
      this.tickWatchTimer = null
    }
  }

  private scheduleReconnect(): void {
    if (!this.autoReconnect) return
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = setTimeout(() => this.doConnect(), this.reconnectDelay)
    this.reconnectDelay = Math.min(
      this.reconnectDelay * this.reconnectFactor,
      this.maxReconnectDelay,
    )
  }

  private setState(s: RpcState): void {
    if (this.stateValue === s) return
    this.stateValue = s
    this.emit('_state', s)
  }
}
