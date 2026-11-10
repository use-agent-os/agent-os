/**
 * Runtime signals: what the console knows about the gateway beyond "the
 * socket is open".
 *
 * One store per app. `bindRuntimeSignals(rpc)` feeds it from the
 * connection's own frames (the hello, every event frame, the `tick`
 * revision, a refused handshake) and from two reads: `doctor.status`
 * (shallow) and `GET /ready`. Both reads run on (re)connect and every few
 * ticks; that is what keeps them current with today's gateway. A `health`
 * event or a `state_version.health` bump triggers an extra read, but the
 * gateway does not send either yet, so nothing depends on them. Nothing here
 * runs on an interval timer of its own: when the gateway stops ticking, the
 * signals stop moving, which is exactly what the freshness readout has to
 * show.
 *
 * The connection state itself stays in `useConnection` (both the web
 * providers and the desktop's mirror the client's `_state` into it); this
 * store holds the rest. The derivations that read it live in
 * runtime-truth.ts and runtime-issues.ts and are pure.
 *
 * Kept free of view imports on purpose: the shell loads it in the entry
 * bundle, so it must not drag a view's copy along.
 */
import { create } from 'zustand'
import { apiUrl } from '@/lib/api-origin'
import { authenticatedHeaders } from '@/lib/http-auth'
import type { RpcState } from '@/lib/ws-rpc'

/** The protocol version this console speaks (the handshake asks for 3..3). */
export const EXPECTED_PROTOCOL = 3

/** Default `tick` cadence when the hello carries no policy. */
export const DEFAULT_TICK_MS = 30_000

/** Re-read doctor.status / ready every Nth tick (4 x 30 s = 2 min by default). */
export const DOCTOR_EVERY_TICKS = 4

/** A connected view with no frame for this long is a snapshot, not live. */
export function staleAfterMs(tickIntervalMs: number): number {
  const tick = tickIntervalMs > 0 ? tickIntervalMs : DEFAULT_TICK_MS
  return Math.max(10_000, tick * 2.5)
}

/** A frame-age update is recorded at most this often (streams are chatty). */
const FRAME_THROTTLE_MS = 1_000

/** A doctor.status finding, as the gateway sends it. Structural on purpose. */
export interface DoctorFinding {
  id?: string
  severity?: string
  readinessImpact?: string
  surface?: string
  title?: string
  detail?: string
  evidence?: Record<string, unknown>
  fixSteps?: Array<{ label?: string; command?: string; detail?: string }>
  restartRequired?: boolean
}

export interface DoctorSummary {
  status: string
  ready: boolean
  counts: Record<string, number>
  findings: DoctorFinding[]
  fetchedAt: number
}

/**
 * `GET /ready`. Older gateways answer `{ready, status}`; newer ones add
 * `state: "starting"|"ready"|"draining"|"safe_mode"` and `failing: []`.
 * Both shapes normalise to this.
 */
export interface ReadyProbe {
  ready: boolean
  state: string
  failing: string[]
  fetchedAt: number
}

export type HandshakeKind = 'auth_failed' | 'auth_rate_limited' | 'protocol_mismatch' | 'refused'

/**
 * A refused handshake, from the RpcError the client emits as
 * `_handshakeError`: the category `code` (UNAUTHORIZED, INVALID_REQUEST, …)
 * and the specific reason in `details.code` (PROTOCOL_MISMATCH,
 * AUTH_RATE_LIMITED, …).
 */
export interface HandshakeFailure {
  kind: HandshakeKind
  code: string
  detailCode: string
  message: string
  /** AUTH_RATE_LIMITED: when the gateway accepts another attempt. */
  retryAfterMs: number | null
  /** PROTOCOL_MISMATCH: the console is older than the gateway; a reload fixes it. */
  reloadRequired: boolean
  at: number
}

/** `shutdown {reason, restart_expected_ms}`: the gateway said it is going away. */
export interface ShutdownNotice {
  reason: string
  /** In-place restart: when it expects to be back. Null for a terminal stop. */
  restartExpectedMs: number | null
  at: number
}

export interface RuntimeSignalsState {
  everConnected: boolean
  /**
   * Connection attempts that ended without a connection since the last
   * success. Non-zero means "tried and could not reach it", which is
   * different from "has not tried yet" on a fresh page.
   */
  failedAttempts: number
  connectedAt: number | null
  /** Epoch ms of the last frame the gateway sent this console. */
  lastFrameAt: number | null
  tickIntervalMs: number
  /** Bumped on every `tick`; the inbox and the doctor refresh key off it. */
  tickRevision: number
  stateVersion: Record<string, number> | null
  protocol: number | null
  serverVersion: string | null
  /** `hello-ok.features.methods`; null before the first hello. */
  methods: string[] | null
  /** `hello-ok.features.capabilities`; null when the gateway does not send it. */
  capabilities: string[] | null
  /** The last refused handshake; cleared by the next successful one. */
  handshake: HandshakeFailure | null
  /** The last `shutdown` notice; cleared by the next connection. */
  shutdown: ShutdownNotice | null
  doctor: DoctorSummary | null
  doctorError: string | null
  ready: ReadyProbe | null
  /** The last failed update, reported by whichever surface ran it. */
  updateFailure: { detail: string; at: number } | null
  /** Re-read doctor.status and /ready now. A no-op until a binder is attached. */
  refresh(): void
  /**
   * Reconnect now (skips backoff); forces a new socket when the connected one
   * has gone silent. A no-op until a binder is attached.
   */
  retry(): void
  noteUpdateFailure(detail: string | null): void
  reset(): void
}

const INITIAL = {
  everConnected: false,
  failedAttempts: 0,
  connectedAt: null,
  lastFrameAt: null,
  tickIntervalMs: DEFAULT_TICK_MS,
  tickRevision: 0,
  stateVersion: null,
  protocol: null,
  serverVersion: null,
  methods: null,
  capabilities: null,
  handshake: null,
  shutdown: null,
  doctor: null,
  doctorError: null,
  ready: null,
  updateFailure: null,
} satisfies Partial<RuntimeSignalsState>

export const useRuntimeSignals = create<RuntimeSignalsState>((set) => ({
  ...INITIAL,
  refresh: () => {},
  retry: () => {},
  noteUpdateFailure: (detail) => set({ updateFailure: detail ? { detail, at: Date.now() } : null }),
  reset: () => set({ ...INITIAL }),
}))

/**
 * Read the gateway's typed refusal (category `code` + `details.code`). The
 * message is only a fallback for an older gateway that sends no details.
 */
export function handshakeFailure(raw: unknown, at: number): HandshakeFailure {
  const err = (raw ?? {}) as { code?: unknown; message?: unknown; details?: unknown }
  const details = (err.details && typeof err.details === 'object' ? err.details : {}) as Record<
    string,
    unknown
  >
  const code = typeof err.code === 'string' ? err.code : ''
  const detailCode = typeof details.code === 'string' ? details.code : ''
  const message = typeof err.message === 'string' ? err.message : ''
  const retry = Number(details.retry_after_ms)
  let kind: HandshakeKind = 'refused'
  if (detailCode === 'PROTOCOL_MISMATCH' || (!detailCode && /protocol/i.test(message))) {
    kind = 'protocol_mismatch'
  } else if (detailCode === 'AUTH_RATE_LIMITED') {
    kind = 'auth_rate_limited'
  } else if (code === 'UNAUTHORIZED' || (!code && /\bauth/i.test(message))) {
    kind = 'auth_failed'
  }
  return {
    kind,
    code,
    detailCode,
    message,
    retryAfterMs: Number.isFinite(retry) && retry > 0 ? retry : null,
    reloadRequired: details.reload_required === true,
    at,
  }
}

/** Read a `shutdown` event payload. */
export function shutdownNotice(raw: unknown, at: number): ShutdownNotice {
  const p = (raw ?? {}) as { reason?: unknown; restart_expected_ms?: unknown }
  const ms = Number(p.restart_expected_ms)
  return {
    reason: typeof p.reason === 'string' ? p.reason : '',
    restartExpectedMs: p.restart_expected_ms != null && Number.isFinite(ms) && ms > 0 ? ms : null,
    at,
  }
}

function numberRecord(raw: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (!raw || typeof raw !== 'object') return out
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value === 'number' && Number.isFinite(value)) out[key] = value
  }
  return out
}

function stringList(raw: unknown): string[] | null {
  if (!Array.isArray(raw)) return null
  return raw.filter((v): v is string => typeof v === 'string')
}

/** doctor.status payload -> summary. Tolerates a partial or odd payload. */
export function normalizeDoctor(raw: unknown, now: number): DoctorSummary {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const findings = Array.isArray(r.findings)
    ? (r.findings.filter((f) => f && typeof f === 'object') as DoctorFinding[])
    : []
  return {
    status: typeof r.status === 'string' ? r.status : '',
    ready: r.ready === true,
    counts: numberRecord(r.counts),
    findings,
    fetchedAt: now,
  }
}

/** /ready body (old boolean shape or the newer state/failing shape) -> probe. */
export function normalizeReady(raw: unknown, now: number): ReadyProbe | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const ready = r.ready === true
  const state =
    typeof r.state === 'string'
      ? r.state
      : typeof r.status === 'string'
        ? r.status
        : ready
          ? 'ready'
          : 'starting'
  return { ready, state, failing: stringList(r.failing) ?? [], fetchedAt: now }
}

/** Error-level findings in a doctor summary (counts first, findings as fallback). */
export function doctorErrorCount(doctor: DoctorSummary | null): number {
  if (!doctor) return 0
  const counted = doctor.counts.error
  if (typeof counted === 'number') return counted
  return doctor.findings.filter((f) => f.severity === 'error').length
}

/** The slice of the RPC client the binder needs; the tests pass a fake. */
export interface SignalsRpc {
  readonly state: RpcState
  on(event: string, handler: (...args: unknown[]) => void): () => void
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
  reconnect(options?: { force?: boolean }): void
}

export interface BindRuntimeSignalsOptions {
  now?: () => number
  /** Re-read every Nth tick. */
  doctorEveryTicks?: number
  /** Read `/ready`; null skips the probe. Defaults to an authenticated GET. */
  fetchReady?: (() => Promise<unknown>) | null
}

async function defaultFetchReady(): Promise<unknown> {
  const url = apiUrl('/ready')
  const resp = await fetch(url, {
    cache: 'no-store',
    headers: authenticatedHeaders(url),
  })
  // 503 while starting still carries the JSON body the probe wants.
  return resp.json()
}

/**
 * Attach the store to one RPC client. Returns the disposer; the store keeps
 * its last values (a snapshot) after the client goes away.
 */
export function bindRuntimeSignals(
  rpc: SignalsRpc,
  options: BindRuntimeSignalsOptions = {},
): () => void {
  const now = options.now ?? Date.now
  const every = Math.max(1, options.doctorEveryTicks ?? DOCTOR_EVERY_TICKS)
  const fetchReady = options.fetchReady === undefined ? defaultFetchReady : options.fetchReady
  const store = useRuntimeSignals
  let disposed = false
  let doctorBusy = false
  let readyBusy = false
  let ticks = 0

  const refreshDoctor = (): void => {
    if (disposed || doctorBusy || rpc.state !== 'connected') return
    doctorBusy = true
    rpc
      .call('doctor.status', { agentId: 'main', deep: false })
      .then((raw) => {
        if (!disposed) store.setState({ doctor: normalizeDoctor(raw, now()), doctorError: null })
      })
      .catch((err: unknown) => {
        if (disposed) return
        store.setState({ doctorError: err instanceof Error ? err.message : String(err) })
      })
      .finally(() => {
        doctorBusy = false
      })
  }

  const refreshReady = (): void => {
    if (disposed || readyBusy || !fetchReady) return
    readyBusy = true
    fetchReady()
      .then((raw) => {
        const probe = normalizeReady(raw, now())
        if (!disposed && probe) store.setState({ ready: probe })
      })
      .catch(() => {
        // An unreachable /ready says nothing the socket does not already say.
      })
      .finally(() => {
        readyBusy = false
      })
  }

  const refresh = (): void => {
    refreshDoctor()
    refreshReady()
  }

  const noteFrame = (): void => {
    const at = now()
    const last = store.getState().lastFrameAt
    if (last === null || at - last >= FRAME_THROTTLE_MS) store.setState({ lastFrameAt: at })
  }

  let previous: unknown = rpc.state
  const unsubs = [
    rpc.on('_state', (state) => {
      const was = previous
      previous = state
      if (state === 'disconnected' && was === 'connecting') {
        store.setState((s) => ({ failedAttempts: s.failedAttempts + 1 }))
        return
      }
      if (state !== 'connected') return
      store.setState({
        everConnected: true,
        failedAttempts: 0,
        connectedAt: now(),
        lastFrameAt: now(),
        handshake: null,
        shutdown: null,
      })
    }),
    rpc.on('_hello', (raw) => {
      const hello = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
      const features = (hello.features ?? {}) as Record<string, unknown>
      const snapshot = (hello.snapshot ?? {}) as Record<string, unknown>
      const policy = (hello.policy ?? {}) as Record<string, unknown>
      const server = (hello.server ?? {}) as Record<string, unknown>
      const tick = policy.tick_interval_ms
      store.setState({
        everConnected: true,
        lastFrameAt: now(),
        handshake: null,
        protocol: typeof hello.protocol === 'number' ? hello.protocol : null,
        serverVersion: typeof server.version === 'string' ? server.version : null,
        methods: stringList(features.methods),
        capabilities: stringList(features.capabilities),
        stateVersion: snapshot.state_version ? numberRecord(snapshot.state_version) : null,
        tickIntervalMs: typeof tick === 'number' && tick > 0 ? tick : DEFAULT_TICK_MS,
      })
      ticks = 0
      refresh()
    }),
    rpc.on('_handshakeError', (raw) => {
      store.setState({ handshake: handshakeFailure(raw, now()) })
    }),
    rpc.on('_state_version', (raw) => {
      const next = numberRecord(raw)
      const prev = store.getState().stateVersion
      store.setState({ stateVersion: { ...(prev ?? {}), ...next } })
      if (next.health !== undefined && next.health !== prev?.health) refreshDoctor()
    }),
    rpc.on('*', (event) => {
      noteFrame()
      if (event === 'tick') {
        ticks += 1
        store.setState((s) => ({ tickRevision: s.tickRevision + 1 }))
        if (ticks % every === 0) refresh()
      } else if (event === 'health') {
        refreshDoctor()
      }
    }),
    rpc.on('shutdown', (payload) => {
      store.setState({ shutdown: shutdownNotice(payload, now()) })
    }),
  ]

  // Retry reconnects at once. A socket that still reads "connected" but has
  // gone silent (the stale state the badge reports) is dropped and reopened;
  // a healthy one is left alone.
  const retry = (): void => {
    const s = store.getState()
    const stale =
      rpc.state === 'connected' &&
      (s.lastFrameAt === null || now() - s.lastFrameAt > staleAfterMs(s.tickIntervalMs))
    rpc.reconnect({ force: stale })
  }
  store.setState({ refresh, retry })
  // Bound to an already-open client (a remount): read once now.
  if (rpc.state === 'connected') {
    store.setState({ everConnected: true, lastFrameAt: now() })
    refresh()
  }

  return () => {
    disposed = true
    for (const off of unsubs) off()
    store.setState({ refresh: () => {}, retry: () => {} })
  }
}
