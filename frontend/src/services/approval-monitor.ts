/**
 * AgentOS Control — global approval-prompt monitor service.
 *
 * Typed port of legacy static/js/approval_monitor.js (271 lines). This is a
 * REST-polling service (NOT WS-RPC): it polls GET /api/approvals with an
 * adaptive 1500ms→30000ms backoff, re-polls immediately on window focus /
 * visibilitychange, and resolves approvals via POST /api/approvals/resolve.
 *
 * The pending list + count are published to a zustand store (useApprovals) so
 * the nav badge and the <ApprovalPrompt /> modal render off React state instead
 * of the legacy imperative DOM writes (_setBadge / _openModal). elevatedMode
 * persistence, the toast-on-new-pending hook, and pollNow() are preserved 1:1.
 */
import { create } from 'zustand'
import { toast } from 'sonner'

// approval_monitor.js:4-8 — polling cadence + elevated-mode storage constants.
const POLL_MS = 1500
const POLL_MAX_MS = 30000
export const ELEVATED_MODE_KEY = 'agentos.elevatedMode'
export const ELEVATED_MODE_VERSION_KEY = 'agentos.elevatedMode.version'
export const ELEVATED_MODE_STORAGE_VERSION = '2'

// app.py:289-335 — the enriched pending item shape returned by GET /api/approvals.
export interface Approval {
  id: string
  namespace?: string
  toolName?: string
  actionKind?: string
  sessionKey?: string
  agent?: string
  command?: string
  warning?: string
  argv?: unknown[]
  args?: unknown
  params?: unknown
  mode?: string
  created_at?: number
}

export interface ApprovalsResponse {
  pending?: Approval[]
  mode?: string
  allowPatterns?: unknown[]
  denyPatterns?: unknown[]
}

export type ElevatedMode = 'on' | 'bypass' | 'full' | ''

// The reactive surface consumed by the nav badge + modal. The legacy service
// pushed the same data through a CustomEvent ('agentos:approvals-pending') and
// imperative DOM writes; the store replaces both.
interface ApprovalsState {
  pending: Approval[]
  count: number
  mode: string
  // The prompt the modal shows: the first pending item + its display mode, or
  // null when nothing is pending (approval_monitor.js:90-91 — legacy only
  // opened the modal for pending[0] and only when no modal was already open).
  setFromPoll(pending: Approval[], mode: string): void
  clear(): void
}

export const useApprovals = create<ApprovalsState>((set) => ({
  pending: [],
  count: 0,
  mode: 'prompt',
  setFromPoll: (pending, mode) => set({ pending, count: pending.length, mode }),
  clear: () => set({ pending: [], count: 0 }),
}))

// approval_monitor.js:56-61 — Authorization header from the per-tab session
// token. Legacy read App.getAuthToken() → sessionStorage['agentos.wsToken']
// (app.js:205-213); storage access is guarded like every other reader.
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) }
  let token = ''
  try {
    token = sessionStorage.getItem('agentos.wsToken') || ''
  } catch {
    /* storage unavailable */
  }
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

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

export type ApprovalAction = 'once' | 'always' | 'bypass' | 'deny'

/**
 * The approval-monitor singleton. start() begins polling + wires focus/
 * visibility re-poll; stop() tears everything down. pollNow() is the re-poll
 * hook the approvals view calls after it mutates settings. resolve() posts a
 * decision and re-polls. The class is instantiated once (approvalMonitor) but
 * kept a class so tests can spin up isolated instances with injected timers.
 */
export class ApprovalMonitor {
  private timer: ReturnType<typeof setTimeout> | null = null
  private busy = false
  private pollBusy = false
  private pollDelayMs = POLL_MS
  private started = false
  private lastToastCount = 0

  private onFocus = (): void => {
    // approval_monitor.js:107-110
    this.resetPollBackoff()
    void this.poll()
  }

  private onVisibilityChange = (): void => {
    // approval_monitor.js:100-105
    if (document.visibilityState === 'visible') {
      this.resetPollBackoff()
      void this.poll()
    }
  }

  // approval_monitor.js:17-23
  start(): void {
    if (this.started) return
    this.started = true
    this.schedulePoll(0)
    window.addEventListener('focus', this.onFocus)
    document.addEventListener('visibilitychange', this.onVisibilityChange)
  }

  // approval_monitor.js:25-32
  stop(): void {
    this.started = false
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    window.removeEventListener('focus', this.onFocus)
    document.removeEventListener('visibilitychange', this.onVisibilityChange)
    useApprovals.getState().clear()
  }

  // approval_monitor.js:34-36 — the re-poll hook consumed by the approvals view.
  async pollNow(): Promise<void> {
    await this.poll()
  }

  // approval_monitor.js:38-46 — self-rescheduling poll loop; each tick polls
  // then re-arms at the current (possibly backed-off) delay.
  private schedulePoll(delayMs: number = this.pollDelayMs): void {
    if (!this.started) return
    if (this.timer) clearTimeout(this.timer)
    this.timer = setTimeout(() => {
      this.timer = null
      void this.poll().then(() => this.schedulePoll(this.pollDelayMs))
    }, delayMs)
  }

  // approval_monitor.js:48-50
  private resetPollBackoff(): void {
    this.pollDelayMs = POLL_MS
  }

  // approval_monitor.js:52-54 — exponential backoff, clamped to [POLL_MS, MAX].
  private increasePollBackoff(): void {
    this.pollDelayMs = Math.min(POLL_MAX_MS, Math.max(POLL_MS, this.pollDelayMs * 2))
  }

  // approval_monitor.js:63-98 — the poll body. Reentrancy-guarded via pollBusy.
  private async poll(): Promise<void> {
    if (this.pollBusy) return
    this.pollBusy = true
    try {
      const resp = await fetch(approvalsUrl(), {
        cache: 'no-store',
        headers: authHeaders(),
      })
      if (!resp.ok) {
        useApprovals.getState().setFromPoll([], useApprovals.getState().mode)
        this.increasePollBackoff()
        return
      }
      const data = (await resp.json()) as ApprovalsResponse
      const pending = Array.isArray(data.pending) ? data.pending : []
      const mode = data.mode || 'prompt'
      useApprovals.getState().setFromPoll(pending, mode)

      // approval_monitor.js:80-81 — pending resets the backoff, empty grows it.
      if (pending.length > 0) this.resetPollBackoff()
      else this.increasePollBackoff()

      // approval_monitor.js:83-88 — toast once per new pending count.
      if (pending.length > 0 && pending.length !== this.lastToastCount) {
        this.lastToastCount = pending.length
        toast.warning('Approval required', { id: 'approval-required', duration: 2500 })
      } else if (pending.length === 0) {
        this.lastToastCount = 0
      }
    } catch {
      useApprovals.getState().setFromPoll([], useApprovals.getState().mode)
      this.increasePollBackoff()
    } finally {
      this.pollBusy = false
    }
  }

  /**
   * approval_monitor.js:171-220 — resolve a pending approval. Maps the modal
   * button action to the (approved, allowAlways, rememberIntent, elevatedMode)
   * tuple, POSTs it, persists elevated mode on success, toasts the outcome, and
   * re-polls. Reentrancy-guarded via `busy`. Rejects on HTTP error so the modal
   * can re-enable its buttons; resolves on success.
   */
  async resolve(item: Approval, action: ApprovalAction): Promise<void> {
    if (this.busy) return
    this.busy = true
    // approval_monitor.js:173-177
    const approved = action === 'once' || action === 'always' || action === 'bypass'
    const allowAlways = action === 'always'
    const rememberIntent = action === 'always'
    const elevatedMode: ElevatedMode = action === 'bypass' ? 'bypass' : ''
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
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      })
      if (!resp.ok) throw new Error('HTTP ' + resp.status)
      if (elevatedMode) setBrowserElevated(elevatedMode)
      // approval_monitor.js:207-211 — outcome toast (info on approve, warn on deny).
      if (elevatedMode) {
        toast.warning('Approval bypass enabled', { id: 'approval-outcome', duration: 2500 })
      } else if (approved) {
        toast.success('Approval granted', { id: 'approval-outcome', duration: 2500 })
      } else {
        toast.warning('Approval denied', { id: 'approval-outcome', duration: 2500 })
      }
      this.resetPollBackoff()
      // approval_monitor.js:213 — legacy re-polled after 150ms; poll immediately
      // (the store update is what the modal reacts to, no DOM settle to wait on).
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

/**
 * approval_monitor.js:67 — the poll endpoint. Legacy fetched the ROOT-absolute
 * '/api/approvals'; these routes are registered at the gateway root (app.py:
 * 535-537), NOT under control_ui.base_path, and the rate-limit exemption keys
 * off the bare '/api/approvals' (middleware.py:240). So — unlike /api/bootstrap
 * which lives under base_path — the approvals REST surface is root-absolute and
 * must NOT be rewritten through the BASE_URL-derived base. We keep the legacy
 * root-absolute path verbatim.
 */
export function approvalsUrl(): string {
  return '/api/approvals'
}

export function approvalsResolveUrl(): string {
  return '/api/approvals/resolve'
}

// The app-wide singleton wired by AppProviders (start on mount, stop on unmount).
export const approvalMonitor = new ApprovalMonitor()
