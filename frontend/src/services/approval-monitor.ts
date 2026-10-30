/**
 * AgentOS Control — global approval-prompt monitor service.
 *
 * Event-driven: the gateway pushes `approval.requested` / `approval.resolved`
 * over the Control WebSocket whenever the approval queue changes (every
 * namespace: exec, plugin, acp, acp_elicitation, flow), and the monitor applies each
 * frame to the store as it arrives. REST is only the backfill: one
 * GET /api/approvals on every transition of the WS client to 'connected' (and
 * once at start when it is already connected), on window focus /
 * visibilitychange, after a resolve, and whenever a caller asks via pollNow().
 * There is no interval timer. Decisions still go out over
 * POST /api/approvals/resolve.
 *
 * A backfill is authoritative and replaces `pending` wholesale, except that an
 * event that lands while the fetch is in flight wins over the (possibly older)
 * snapshot: an id requested meanwhile is merged back in, an id resolved
 * meanwhile is dropped.
 *
 * The pending list + count are published to a zustand store (useApprovals) so
 * the nav badge and the <ApprovalPrompt /> modal render off React state instead
 * of the legacy imperative DOM writes (_setBadge / _openModal). elevatedMode
 * persistence, the toast-on-new-pending hook, and pollNow() are preserved.
 */
import { create } from 'zustand'
import { apiUrl } from '@/lib/api-origin'
import { authenticatedHeaders } from '@/lib/http-auth'
import { toast } from 'sonner'

// approval_monitor.js:4-8 — elevated-mode storage constants.
export const ELEVATED_MODE_KEY = 'agentos.elevatedMode'
export const ELEVATED_MODE_VERSION_KEY = 'agentos.elevatedMode.version'
export const ELEVATED_MODE_STORAGE_VERSION = '2'

// app.py api_approvals — the enriched pending item shape returned by
// GET /api/approvals (and carried verbatim as `approval` on an
// `approval.requested` frame).
export interface Approval {
  id: string
  namespace?: string
  toolName?: string
  actionKind?: string
  sessionKey?: string
  /**
   * The requesting session followed by its subagent / coding-agent ancestors
   * (gateway `audience_session_keys`): a parent session's viewers see a
   * child's prompt too.
   */
  audienceSessionKeys?: string[]
  agent?: string
  command?: string
  warning?: string
  argv?: unknown[]
  args?: unknown
  params?: unknown
  mode?: string
  created_at?: number
  title?: string
}

export interface ApprovalsResponse {
  pending?: Approval[]
  mode?: string
  allowPatterns?: unknown[]
  denyPatterns?: unknown[]
}

export type ElevatedMode = 'on' | 'bypass' | 'full' | ''

/**
 * The slice of the WS-RPC client the monitor needs (WsRpcClient satisfies it).
 * `state`, when present, lets start() backfill at once if the client is
 * already connected instead of waiting for the next 'connected' transition.
 */
export interface ApprovalEventSource {
  on(event: string, handler: (...args: unknown[]) => void): () => void
  readonly state?: string
}

// The reactive surface consumed by the nav badge + modal. The legacy service
// pushed the same data through a CustomEvent ('agentos:approvals-pending') and
// imperative DOM writes; the store replaces both.
//
// `count` backs the nav badge (legacy #approval-count); `pending` backs the
// modal (legacy pinned _modal to pending[0] at open). These are deliberately
// decoupled: a transient backfill failure must zero the BADGE without
// dismissing an OPEN prompt (approval_monitor.js:71-74,92-97 — the error/non-ok
// branch only calls _setBadge(0) and NEVER touches _modal). So failure paths
// call zeroBadge() (count → 0, pending untouched); a successful backfill
// replaces the pending list via setFromPoll(), and pushed events edit it one
// item at a time via upsert() / remove().
interface ApprovalsState {
  pending: Approval[]
  count: number
  mode: string
  // The browser session elevated mode (approval_monitor.js:227-239 / legacy
  // approvals.js:234-243). Kept in the store so the approvals view's
  // "Effective execution mode" readout re-renders reactively after an in-view
  // Bypass resolve persists it (setBrowserElevated) — legacy re-ran _loadData
  // after resolve; the store now backs that refresh live. Hydrated from
  // localStorage on module load and kept in lockstep with it by
  // setBrowserElevated. This is the concrete consumer that retires the deferred
  // 'agentos:elevated-mode' CustomEvent — the store path replaces it.
  elevatedMode: ElevatedMode
  setFromPoll(pending: Approval[], mode: string): void
  // Insert a pushed approval, or replace the queued item with the same id in
  // place; new ids append, so the queue keeps arrival order.
  upsert(item: Approval): void
  // Drop the item with this id (resolved, expired, or decided here).
  remove(id: string): void
  // approval_monitor.js:71-74,92-97 — zero the badge count on backfill failure
  // while leaving any pending item backing an open prompt intact.
  zeroBadge(): void
  setElevatedMode(mode: ElevatedMode): void
  clear(): void
}

/**
 * approvals.js:234-243 — read the persisted browser elevated mode, downgrading a
 * legacy 'full' written under an older storage version to 'bypass'. Used to
 * hydrate the store on load so the readout starts from the stored value. (The
 * approvals view re-exports this as browserElevatedMode() from its logic.ts.)
 */
// approvals.js:245-247 / chat.js:2217-2219 (_normalizeElevatedMode) — only
// on/bypass/full are valid elevated modes; anything else is the empty string.
// The single source of truth for the elevated-mode model (shared by the chat
// toolbar + the approvals readout, both of which re-export it from here).
export function normalizeElevatedMode(mode: string | null | undefined): ElevatedMode {
  return mode === 'on' || mode === 'bypass' || mode === 'full' ? mode : ''
}

// chat.js:2225-2227 (_isApprovalBypassMode) — bypass + full skip approval
// prompts; on/'' keep them. Shared with the chat toolbar (which derives the
// glow/dot state from the effective mode) and the approvals readout, so it
// lives in the single elevated-mode source of truth alongside the reader.
export function isApprovalBypassMode(mode: string | null | undefined): boolean {
  return mode === 'bypass' || mode === 'full'
}

export function readBrowserElevated(): ElevatedMode {
  let mode = ''
  let version = ''
  try {
    mode = localStorage.getItem(ELEVATED_MODE_KEY) || ''
    version = localStorage.getItem(ELEVATED_MODE_VERSION_KEY) || ''
  } catch {
    /* storage unavailable */
  }
  if (mode === 'full' && version !== ELEVATED_MODE_STORAGE_VERSION) return 'bypass'
  return normalizeElevatedMode(mode)
}

export const useApprovals = create<ApprovalsState>((set) => ({
  pending: [],
  count: 0,
  mode: 'prompt',
  elevatedMode: readBrowserElevated(),
  setFromPoll: (pending, mode) => set({ pending, count: pending.length, mode }),
  upsert: (item) =>
    set((state) => {
      const at = state.pending.findIndex((p) => p.id === item.id)
      const pending =
        at === -1 ? [...state.pending, item] : state.pending.map((p, i) => (i === at ? item : p))
      return { pending, count: pending.length }
    }),
  remove: (id) =>
    set((state) => {
      if (!state.pending.some((p) => p.id === id)) return state
      const pending = state.pending.filter((p) => p.id !== id)
      return { pending, count: pending.length }
    }),
  zeroBadge: () => set({ count: 0 }),
  setElevatedMode: (mode) => set({ elevatedMode: mode }),
  clear: () => set({ pending: [], count: 0 }),
}))

// approval_monitor.js:227-239 — normalize + persist the browser elevated mode
// under storage version '2'. Only on/bypass/full are valid; anything else
// clears the keys. Returns the normalized value for the resolve toast copy.
export function setBrowserElevated(mode: string): ElevatedMode {
  const normalized: ElevatedMode = mode === 'full' || mode === 'bypass' || mode === 'on' ? mode : ''
  try {
    if (normalized) {
      localStorage.setItem(ELEVATED_MODE_KEY, normalized)
      localStorage.setItem(ELEVATED_MODE_VERSION_KEY, ELEVATED_MODE_STORAGE_VERSION)
    } else {
      localStorage.removeItem(ELEVATED_MODE_KEY)
      localStorage.removeItem(ELEVATED_MODE_VERSION_KEY)
    }
  } catch {
    /* storage unavailable */
  }
  // Keep the reactive store in lockstep with localStorage so the approvals
  // view's effective-execution-mode readout re-renders after an in-view Bypass
  // resolve (the concrete consumer that replaces the deferred CustomEvent).
  useApprovals.getState().setElevatedMode(normalized)
  return normalized
}

// approval_monitor.js:241-246 — derive the command line shown in the modal.
export function approvalCommand(item: Approval): string {
  if (item.command) return String(item.command)
  if (Array.isArray(item.argv) && item.argv.length > 0) return item.argv.map(String).join(' ')
  const args = item.args as { command?: unknown } | null | undefined
  if (args && args.command) return String(args.command)
  return ''
}

// approval_monitor.js:248-258 — detail body: warning text, else pretty-printed
// args/params truncated at 900 chars.
export function approvalDetail(item: Approval): string {
  if (item.warning) return String(item.warning)
  const args = item.args ?? item.params ?? null
  if (!args) return ''
  try {
    const text = JSON.stringify(args, null, 2)
    return text.length > 900 ? text.slice(0, 900) + '...' : text
  } catch {
    return String(args)
  }
}

// approval_monitor.js:148-152 — the modal meta line ("Namespace · Mode · Session").
export function approvalMeta(item: Approval, mode: string): string {
  return [
    item.namespace ? 'Namespace: ' + item.namespace : '',
    mode ? 'Mode: ' + mode : '',
    item.sessionKey ? 'Session: ' + item.sessionKey : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

// approval_monitor.js:145 — "Always Allow This Type" only offered for exec
// commands.
export function canAlwaysAllow(item: Approval): boolean {
  return item.namespace === 'exec' && !!item.command
}

/**
 * Namespaces answered once at a time: an agent's Image Flow spend (`flow`,
 * the `flow_run` cost gate, docs/flows.md) offers only allow-once and deny.
 * "Always allow" or "Stop asking" there would read as unlimited agent spend;
 * the gateway ignores both for this namespace anyway.
 */
export const ONCE_ONLY_NAMESPACES: ReadonlySet<string> = new Set(['flow'])

/** Whether "Bypass approvals" / "Stop asking" may be offered for `item`. */
export function canBypass(item: Approval): boolean {
  return !ONCE_ONLY_NAMESPACES.has(item.namespace || 'exec')
}

export type ApprovalAction = 'once' | 'always' | 'bypass' | 'deny'

function eventId(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

/**
 * Turn an `approval.requested` payload into a queue item. The gateway attaches
 * the full GET /api/approvals item as `approval` when it has one; otherwise
 * the frame's own summary fields are mapped onto the same shape (session_key →
 * sessionKey, audience_session_keys → audienceSessionKeys, tool → toolName,
 * summary → warning). Returns null for a frame
 * without a usable id.
 */
export function approvalFromEvent(payload: unknown): Approval | null {
  return readRequested(payload)?.item ?? null
}

// `full` is true when the frame carried the complete queue item rather than
// just its summary fields.
function readRequested(payload: unknown): { item: Approval; full: boolean } | null {
  if (!payload || typeof payload !== 'object') return null
  const frame = payload as Record<string, unknown>
  const namespace = typeof frame.namespace === 'string' ? frame.namespace : undefined
  const attached = frame.approval
  if (attached && typeof attached === 'object') {
    const id = eventId((attached as { id?: unknown }).id)
    if (id) {
      const item = { ...(attached as Approval), id }
      if (!item.namespace && namespace) item.namespace = namespace
      return { item, full: true }
    }
  }
  const id = eventId(frame.id)
  if (!id) return null
  const item: Approval = { id }
  if (namespace) item.namespace = namespace
  if (typeof frame.session_key === 'string') item.sessionKey = frame.session_key
  if (Array.isArray(frame.audience_session_keys)) {
    item.audienceSessionKeys = frame.audience_session_keys.filter(
      (key): key is string => typeof key === 'string' && key !== '',
    )
  }
  if (typeof frame.tool === 'string') item.toolName = frame.tool
  if (typeof frame.command === 'string') item.command = frame.command
  if (typeof frame.summary === 'string') item.warning = frame.summary
  if (typeof frame.created_at === 'number') item.created_at = frame.created_at
  return { item, full: false }
}

// Events seen while a backfill fetch is in flight, so its (possibly older)
// snapshot cannot undo them.
interface InFlightEvents {
  added: Map<string, Approval>
  removed: Set<string>
}

/**
 * The approval-monitor singleton. start(rpc) subscribes to the pushed approval
 * events, backfills on every (re)connect, and wires focus/visibility backfill;
 * stop() tears everything down. pollNow() is the backfill hook the approvals
 * view calls after it mutates settings. resolve() posts a decision, drops the
 * item locally, and backfills. The class is instantiated once (approvalMonitor)
 * but kept a class so tests can spin up isolated instances.
 */
export class ApprovalMonitor {
  private busy = false
  private started = false
  private unsubscribers: Array<() => void> = []
  // The running backfill (shared by concurrent callers) and whether another
  // caller asked for a fresh one while it was in flight.
  private inflight: Promise<void> | null = null
  private pollAgain = false
  private inflightEvents: InFlightEvents | null = null
  // Bumped by stop(): a fetch that started before it must not repopulate the
  // store it just cleared (e.g. the desktop switching gateways).
  private generation = 0

  private onFocus = (): void => {
    // approval_monitor.js:107-110
    void this.poll()
  }

  private onVisibilityChange = (): void => {
    // approval_monitor.js:100-105
    if (document.visibilityState === 'visible') void this.poll()
  }

  private onRequested = (payload: unknown): void => {
    const parsed = readRequested(payload)
    if (!parsed) return
    const store = useApprovals.getState()
    const known = store.pending.find((p) => p.id === parsed.item.id)
    // A repeated frame only refreshes the queued item when it carries the full
    // one; a bare summary must not overwrite what a backfill already filled in.
    const item = known && !parsed.full ? known : parsed.item
    if (this.inflightEvents) {
      this.inflightEvents.added.set(item.id, item)
      this.inflightEvents.removed.delete(item.id)
    }
    if (item !== known) store.upsert(item)
    if (!known) this.toastRequired()
  }

  private onResolved = (payload: unknown): void => {
    const id =
      payload && typeof payload === 'object' ? eventId((payload as { id?: unknown }).id) : ''
    if (id) this.dropLocal(id)
  }

  private onState = (state: unknown): void => {
    // Pushed events are lost while the socket is down; the snapshot taken on
    // every reconnect is what catches the queue back up.
    if (state === 'connected') void this.poll()
  }

  /**
   * Begin monitoring. With an rpc client, subscribe to `approval.requested`,
   * `approval.resolved` and `_state`, backfilling on every transition to
   * 'connected' (and right away when the client is already connected or does
   * not report a state). Without one, backfill once. Either way, window focus
   * and the document becoming visible each trigger one backfill.
   */
  start(rpc?: ApprovalEventSource): void {
    if (this.started) return
    this.started = true
    window.addEventListener('focus', this.onFocus)
    document.addEventListener('visibilitychange', this.onVisibilityChange)
    if (!rpc) {
      void this.poll()
      return
    }
    this.unsubscribers.push(
      rpc.on('approval.requested', (payload) => this.onRequested(payload)),
      rpc.on('approval.resolved', (payload) => this.onResolved(payload)),
      rpc.on('_state', (state) => this.onState(state)),
    )
    if (rpc.state === undefined || rpc.state === 'connected') void this.poll()
  }

  // approval_monitor.js:25-32
  stop(): void {
    this.started = false
    this.generation += 1
    this.pollAgain = false
    this.inflightEvents = null
    for (const unsubscribe of this.unsubscribers.splice(0)) unsubscribe()
    window.removeEventListener('focus', this.onFocus)
    document.removeEventListener('visibilitychange', this.onVisibilityChange)
    useApprovals.getState().clear()
  }

  // approval_monitor.js:34-36 — the backfill hook consumed by the approvals view.
  async pollNow(): Promise<void> {
    await this.poll()
  }

  // One backfill. Callers arriving while one is in flight share it and get one
  // more fetch after it, so nobody settles on a snapshot older than their call.
  private poll(): Promise<void> {
    if (this.inflight) {
      this.pollAgain = true
      return this.inflight
    }
    const run = async (): Promise<void> => {
      try {
        do {
          this.pollAgain = false
          await this.fetchPending()
        } while (this.pollAgain)
      } finally {
        this.inflight = null
      }
    }
    this.inflight = run()
    return this.inflight
  }

  // approval_monitor.js:63-98 — the backfill body.
  private async fetchPending(): Promise<void> {
    const generation = this.generation
    const seen: InFlightEvents = { added: new Map(), removed: new Set() }
    this.inflightEvents = seen
    try {
      const resp = await fetch(approvalsUrl(), {
        cache: 'no-store',
        headers: authenticatedHeaders(),
      })
      if (generation !== this.generation) return
      if (!resp.ok) {
        // approval_monitor.js:71-74 — zero the badge only; an open prompt keeps
        // its pinned item (legacy never touched _modal on a failed poll).
        useApprovals.getState().zeroBadge()
        return
      }
      const data = (await resp.json()) as ApprovalsResponse
      if (generation !== this.generation) return
      const snapshot = Array.isArray(data.pending) ? data.pending : []
      const mode = data.mode || 'prompt'
      const pending = mergeSnapshot(snapshot, seen)

      const before = new Set(useApprovals.getState().pending.map((p) => p.id))
      useApprovals.getState().setFromPoll(pending, mode)
      if (pending.some((p) => !before.has(p.id))) this.toastRequired()
    } catch {
      // approval_monitor.js:92-97 — same as the non-ok branch: zero the badge,
      // preserve any pending item behind an open prompt.
      if (generation === this.generation) useApprovals.getState().zeroBadge()
    } finally {
      if (this.inflightEvents === seen) this.inflightEvents = null
    }
  }

  // Remove an id from the queue, and keep an in-flight snapshot from bringing
  // it back.
  private dropLocal(id: string): void {
    if (this.inflightEvents) {
      this.inflightEvents.added.delete(id)
      this.inflightEvents.removed.add(id)
    }
    useApprovals.getState().remove(id)
  }

  // approval_monitor.js:83-88 — one toast per approval that newly enters the
  // queue (a repeated frame or a backfill that only confirms it stays quiet).
  private toastRequired(): void {
    toast.warning('Approval required', { id: 'approval-required', duration: 2500 })
  }

  /**
   * approval_monitor.js:171-220 — resolve a pending approval. Maps the modal
   * button action to the (approved, allowAlways, rememberIntent, elevatedMode)
   * tuple, POSTs it, persists elevated mode on success, drops the item from the
   * store so the prompt closes at once, toasts the outcome, and backfills.
   * Reentrancy-guarded via `busy`. Rejects on HTTP error so the modal can
   * re-enable its buttons; resolves on success.
   */
  async resolve(item: Approval, action: ApprovalAction): Promise<void> {
    if (this.busy) return
    this.busy = true
    // approval_monitor.js:173-177
    const approved = action === 'once' || action === 'always' || action === 'bypass'
    // A once-only approval (an agent's spend) never carries "always" or an
    // elevation, whichever surface sent it.
    const onceOnly = ONCE_ONLY_NAMESPACES.has(item.namespace || 'exec')
    const allowAlways = action === 'always' && !onceOnly
    const rememberIntent = allowAlways
    const elevatedMode: ElevatedMode = action === 'bypass' && !onceOnly ? 'bypass' : ''
    const body: Record<string, unknown> = {
      id: item.id,
      namespace: item.namespace || 'exec',
      approved,
      allowAlways,
      rememberIntent,
    }
    if (elevatedMode) body.elevatedMode = elevatedMode
    try {
      const resp = await fetch(approvalsResolveUrl(), {
        method: 'POST',
        headers: authenticatedHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      })
      if (!resp.ok) throw new Error('HTTP ' + resp.status)
      if (elevatedMode) setBrowserElevated(elevatedMode)
      this.dropLocal(item.id)
      // approval_monitor.js:207-211 — outcome toast. Legacy tone is
      // `approved ? 'info' : 'warn'`; a bypass sets approved=true → 'info', and
      // the port maps info→toast.success. So bypass toasts success, not warning.
      if (elevatedMode) {
        toast.success('Approval bypass enabled', { id: 'approval-outcome', duration: 2500 })
      } else if (approved) {
        toast.success('Approval granted', { id: 'approval-outcome', duration: 2500 })
      } else {
        toast.warning('Approval denied', { id: 'approval-outcome', duration: 2500 })
      }
      // approval_monitor.js:213 — legacy re-polled after 150ms; backfill now
      // (a bypass may have released other queued items server-side).
      await this.poll()
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Approval failed: ' + message, { id: 'approval-error', duration: 4000 })
      throw err instanceof Error ? err : new Error(message)
    } finally {
      this.busy = false
    }
  }
}

// A backfill snapshot with the events that raced it applied on top: ids
// resolved meanwhile are dropped, ids requested meanwhile are kept (appended in
// arrival order when the snapshot predates them). Duplicate ids collapse to
// their first occurrence.
function mergeSnapshot(snapshot: Approval[], seen: InFlightEvents): Approval[] {
  const ids = new Set<string>()
  const merged: Approval[] = []
  for (const item of snapshot) {
    if (!item || seen.removed.has(item.id) || ids.has(item.id)) continue
    ids.add(item.id)
    merged.push(item)
  }
  for (const [id, item] of seen.added) {
    if (ids.has(id)) continue
    ids.add(id)
    merged.push(item)
  }
  return merged
}

/**
 * approval_monitor.js:67 — the backfill endpoint. Legacy fetched the
 * ROOT-absolute '/api/approvals'; these routes are registered at the gateway
 * root (app.py: 535-537), NOT under control_ui.base_path, and the gateway's
 * dedicated approvals rate-limit bucket keys off the bare '/api/approvals'
 * (middleware.py, _APPROVALS_PATH). So — unlike /api/bootstrap which lives
 * under base_path — the approvals REST surface is root-absolute and must NOT
 * be rewritten through the BASE_URL-derived base. We keep the legacy
 * root-absolute path verbatim.
 *
 * That bucket (rate_limit.approvals_max_requests, default 300/min) is a bound,
 * not an exemption. The monitor no longer polls on a timer; it calls this only
 * on (re)connect, focus/visibility, after a resolve and on pollNow(), which
 * keeps it far below the limit.
 */
export function approvalsUrl(): string {
  return apiUrl('/api/approvals')
}

export function approvalsResolveUrl(): string {
  return apiUrl('/api/approvals/resolve')
}

// approvals.js:291 — the approval-strategy settings endpoint. Root-absolute like
// the backfill/resolve endpoints (same rationale as approvalsUrl above).
export function approvalsSettingsUrl(): string {
  return apiUrl('/api/approvals/settings')
}

/**
 * approvals.js:291-301 — persist the approval strategy (prompt / auto-approve /
 * auto-deny). POSTs { mode } to the root-absolute settings endpoint with the
 * session Bearer token, throwing on a non-ok response so the caller can revert
 * its optimistic UI. The caller backfills (pollNow) after a success, mirroring
 * the legacy _loadData() + ApprovalMonitor.pollNow() at approvals.js:298-299.
 */
export async function saveApprovalMode(mode: string): Promise<void> {
  const resp = await fetch(approvalsSettingsUrl(), {
    method: 'POST',
    headers: authenticatedHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ mode }),
  })
  if (!resp.ok) throw new Error('HTTP ' + resp.status)
}

// The app-wide singleton wired by AppProviders / the desktop GatewayProviders
// (start with the WS client on mount, stop on unmount).
export const approvalMonitor = new ApprovalMonitor()
