/** AgentOS Control — WebSocket RPC client. Typed port of legacy static/js/rpc.js. */

export type RpcState = 'disconnected' | 'connecting' | 'connected'

export class RpcError extends Error {
  code?: string
  details?: unknown
}

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void }
type Handler = (...args: unknown[]) => void

/** The `client` block of the connect request: who is connecting (informational). */
export interface ClientInfo {
  id: string
  version: string
  platform: string
  mode: string
}

/** The web console ships inside the engine, so its version is the engine's. */
export const WEB_CLIENT_INFO: ClientInfo = {
  id: 'agentos-web',
  version: 'bundled',
  platform: 'web',
  mode: 'webui',
}

/** Protocol version this client speaks (min = max: no older dialects). */
export const CLIENT_PROTOCOL_VERSION = 3

/**
 * A gateway that is still booting answers `connect` with a retryable
 * UNAVAILABLE whose `details.code` is STARTUP_SIDECARS, then closes 1013.
 * Returns the delay the gateway asked for, or null for any other error.
 */
function startupRetryAfterMs(err: unknown): number | null {
  if (!(err instanceof RpcError) || err.code !== 'UNAVAILABLE') return null
  const details = err.details as { code?: unknown; retry_after_ms?: unknown } | undefined
  if (!details || details.code !== 'STARTUP_SIDECARS') return null
  const ms = Number(details.retry_after_ms)
  return Number.isFinite(ms) && ms > 0 ? ms : 1000
}

export class WsRpcClient {
  private ws: WebSocket | null = null
  private reqId = 0
  private pending = new Map<string, Pending>()
  private listeners = new Map<string, Set<Handler>>()
  private stateValue: RpcState = 'disconnected'
  private url = ''
  private token: string | null = null
  private extraAuth: Record<string, string> | null = null
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
  private challengeNonce: string | null = null
  private capabilitiesValue: string[] = []
  private clientInfo: ClientInfo
  /** Set while a handshake refused with STARTUP_SIDECARS is being retried. */
  private startupRetry = false
  private readonly WebSocketImpl: typeof WebSocket

  constructor(opts?: { WebSocketImpl?: typeof WebSocket; clientInfo?: Partial<ClientInfo> }) {
    this.WebSocketImpl = opts?.WebSocketImpl ?? WebSocket
    this.clientInfo = { ...WEB_CLIENT_INFO, ...(opts?.clientInfo ?? {}) }
  }

  /** Update the `client` block sent on the next handshake (e.g. a late app version). */
  setClientInfo(info: Partial<ClientInfo>): void {
    this.clientInfo = { ...this.clientInfo, ...info }
  }

  /**
   * `extraAuth` rides along in the handshake's `auth` object: the desktop
   * presents its operator secret there so the gateway never mistakes the
   * app's own connection for an agent's (see gateway/agent_surface.py).
   *
   * Any socket already open (or a pending backoff retry) is dropped first, so
   * switching gateway or credentials never leaves the old connection feeding
   * events into the same listeners.
   */
  connect(url: string, token?: string | null, extraAuth?: Record<string, string> | null): void {
    this.url = url
    this.token = token ?? null
    this.extraAuth = extraAuth && Object.keys(extraAuth).length ? { ...extraAuth } : null
    this.autoReconnect = true
    this.clearReconnectTimer()
    this.reconnectDelay = 800
    this.dropSocket()
    this.doConnect()
  }

  disconnect(): void {
    this.autoReconnect = false
    this.clearReconnectTimer()
    this.dropSocket()
  }

  /** The WebSocket URL of the last `connect()` ('' before the first). */
  get currentUrl(): string {
    return this.url
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

  /**
   * Connect again now, with the URL and credentials of the last `connect()`,
   * skipping any pending backoff delay. This is the "Retry connection"
   * action: after a refused handshake the client does not retry by itself.
   *
   * A connection that is up is left alone, unless `force` is set: a socket
   * that still reads "connected" but has gone silent (the gateway process is
   * frozen, the network path is half-open) is dropped and reopened. A forced
   * drop goes through "disconnected" like any other, so everything that
   * resubscribes on (re)connect does so again.
   */
  reconnect(options: { force?: boolean } = {}): void {
    if (!this.url) return
    if (this.stateValue === 'connected' && !options.force) return
    this.autoReconnect = true
    this.clearReconnectTimer()
    this.reconnectDelay = 800
    this.dropSocket()
    this.doConnect()
  }

  /** Epoch ms of the last frame the gateway sent (0 before the first). */
  get lastFrameTime(): number {
    return this.lastFrameAt
  }

  get policy(): Record<string, unknown> {
    return this.policyValue ?? {}
  }

  /** `hello-ok.features.capabilities`: additive features the gateway supports. */
  get capabilities(): string[] {
    return this.capabilitiesValue
  }

  hasCapability(name: string): boolean {
    return this.capabilitiesValue.includes(name)
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
    let ws: WebSocket
    try {
      ws = new this.WebSocketImpl(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = ws
    // Every handler checks it still belongs to the current socket: a socket
    // that was replaced must never clear, close or feed the one after it.
    const current = () => this.ws === ws
    let opened = false

    ws.onopen = () => {
      if (!current()) return
      opened = true
      this.reconnectDelay = 800
      // Don't send connect yet — wait for connect.challenge from server
    }

    ws.onmessage = (ev: MessageEvent) => {
      if (!current()) return
      let data: Record<string, unknown>
      try {
        data = JSON.parse(String(ev.data)) as Record<string, unknown>
      } catch {
        return
      }
      if (!this.noteIncomingFrame(data)) return

      // Handshake: server sends connect.challenge, we reply with connect request
      if (data.type === 'event' && data.event === 'connect.challenge') {
        const challenge = (data.payload as { nonce?: unknown } | undefined) ?? {}
        this.challengeNonce = typeof challenge.nonce === 'string' ? challenge.nonce : null
        const auth: Record<string, string> = { ...(this.extraAuth ?? {}) }
        if (this.token) auth.token = this.token
        const authParams = Object.keys(auth).length ? { auth } : {}
        const id = String(++this.reqId)
        this.pending.set(id, {
          resolve: () => {}, // HelloOk is not a res frame, handled below
          reject: (error) => {
            // The socket closing mid-handshake is not a refusal: onclose
            // reconnects with backoff as for any drop.
            if (!(error instanceof RpcError)) return
            // A gateway still booting asks to be retried: stay "connecting"
            // and try again when it said (supervisor contract).
            const retryMs = startupRetryAfterMs(error)
            if (retryMs !== null && this.autoReconnect) {
              this.startupRetry = true
              this.reconnectDelay = Math.min(retryMs, this.maxReconnectDelay)
              this.ws?.close()
              return
            }
            // A refused handshake (wrong token, rate limit, protocol
            // mismatch) does not heal by retrying: stop, and let the UI read
            // the typed error (code + details) from `_handshakeError`.
            this.emit('_handshakeError', error)
            this.setState('disconnected')
            this.ws?.close()
          },
        })
        this.ws?.send(
          JSON.stringify({
            type: 'req',
            id,
            method: 'connect',
            params: {
              minProtocol: CLIENT_PROTOCOL_VERSION,
              maxProtocol: CLIENT_PROTOCOL_VERSION,
              clientKind: 'control',
              client: { ...this.clientInfo },
              ...authParams,
            },
          }),
        )
        return
      }

      // Handshake: HelloOk frame (has "protocol" field, no "type":"res")
      if (data.protocol !== undefined && this.stateValue === 'connecting') {
        // The gateway echoes the challenge nonce; a different one means this
        // hello answers some other connection's handshake.
        const echoed = (data.auth as { nonce?: unknown } | undefined)?.nonce
        if (typeof echoed === 'string' && this.challengeNonce && echoed !== this.challengeNonce) {
          this.emit('_handshakeError', new RpcError('Handshake nonce mismatch'))
          this.ws?.close()
          return
        }
        const features = (data.features as { capabilities?: unknown } | undefined) ?? {}
        this.capabilitiesValue = Array.isArray(features.capabilities)
          ? features.capabilities.filter((c): c is string => typeof c === 'string')
          : []
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
        if (data.event === 'shutdown') this.noteShutdown(data.payload)
        // The gateway may stamp a state_version revision on an event frame;
        // listeners that cache snapshot-derived state refresh when it moves.
        if (data.state_version && typeof data.state_version === 'object') {
          this.emit('_state_version', data.state_version)
        }
        const meta = (data.meta as Record<string, unknown>) ?? {}
        this.emit(String(data.event), data.payload, meta)
        this.emit('*', String(data.event), data.payload, meta)
      }
    }

    ws.onclose = () => {
      if (!current()) return
      this.stopPing()
      this.stopTickWatch()
      for (const [, p] of this.pending) p.reject(new Error('Connection closed'))
      this.pending.clear()
      this.ws = null
      // Closed before it ever opened: nothing answered at that address
      // (refused, unresolvable, blocked). Reconnecting goes on as usual.
      if (!opened) this.emit('_unreachable', this.url)
      if (this.startupRetry) {
        this.startupRetry = false
        this.scheduleReconnect()
        return
      }
      if (this.stateValue !== 'disconnected') {
        this.setState('disconnected')
        this.scheduleReconnect()
      }
    }

    ws.onerror = () => {}
  }

  /**
   * `shutdown {reason, restart_expected_ms}` precedes close 1012. A number
   * means an in-place restart: reconnect when the gateway expects to be back
   * instead of walking the backoff. `null` is a terminal stop; the normal
   * backoff applies (a supervisor may still bring it back).
   */
  private noteShutdown(payload: unknown): void {
    const ms = Number((payload as { restart_expected_ms?: unknown } | null)?.restart_expected_ms)
    if (Number.isFinite(ms) && ms > 0) {
      this.reconnectDelay = Math.min(Math.max(ms, 500), this.maxReconnectDelay)
    }
  }

  /**
   * Let go of the current socket without waiting for its close handshake (a
   * silent peer may never answer it), settle every call that waited on it,
   * and report "disconnected". The caller decides when to connect again.
   */
  private dropSocket(): void {
    this.stopPing()
    this.stopTickWatch()
    if (this.ws) {
      const stale = this.ws
      this.ws = null
      stale.onopen = null
      stale.onmessage = null
      stale.onclose = null
      stale.onerror = null
      try {
        stale.close()
      } catch {
        /* noop */
      }
    }
    for (const [, p] of this.pending) p.reject(new Error('Connection closed'))
    this.pending.clear()
    this.startupRetry = false
    this.setState('disconnected')
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
        // A silent socket is treated as dropped now: waiting for the close
        // handshake of a peer that stopped answering can take another minute.
        this.dropSocket()
        this.scheduleReconnect()
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

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private scheduleReconnect(): void {
    if (!this.autoReconnect) return
    this.clearReconnectTimer()
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.doConnect()
    }, this.reconnectDelay)
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

/** A fresh per-request idempotency key (the gateway dedupes on it). */
export function newIdempotencyKey(): string {
  const cryptoApi = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

/**
 * True for a call that failed in transport (socket down or dropped before the
 * answer), as opposed to an answer from the gateway (`RpcError`). Only the
 * former is worth retrying: the request may or may not have reached it.
 */
export function isTransportError(error: unknown): boolean {
  return error instanceof Error && !(error instanceof RpcError)
}

type RetryableRpc = Pick<WsRpcClient, 'call' | 'waitForConnection'>

/**
 * `rpc.call` for a side-effecting method, safe to retry: the params carry an
 * `idempotency_key` (one is minted when absent) and a transport failure waits
 * for the socket to come back and sends the SAME request again, so a gateway
 * that already acted on the first one answers with that result instead of
 * acting twice. Gateway errors are never retried.
 */
export async function callIdempotent<T = unknown>(
  rpc: RetryableRpc,
  method: string,
  params: Record<string, unknown>,
  opts: { retries?: number; reconnectTimeoutMs?: number } = {},
): Promise<T> {
  const retries = opts.retries ?? 2
  const reconnectTimeoutMs = opts.reconnectTimeoutMs ?? 20000
  const request = { ...params }
  if (typeof request.idempotency_key !== 'string' || !request.idempotency_key) {
    request.idempotency_key = newIdempotencyKey()
  }
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await rpc.call<T>(method, request)
    } catch (error) {
      if (attempt >= retries || !isTransportError(error)) throw error
      let timer: ReturnType<typeof setTimeout> | undefined
      const reconnected = await Promise.race([
        rpc.waitForConnection().then(() => true),
        new Promise<boolean>((resolve) => {
          timer = setTimeout(() => resolve(false), reconnectTimeoutMs)
        }),
      ])
      if (timer !== undefined) clearTimeout(timer)
      if (!reconnected) throw error
    }
  }
}
