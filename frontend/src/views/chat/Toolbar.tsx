import { useCallback, useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'motion/react'
import { toast } from 'sonner'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import {
  approvalMonitor,
  isApprovalBypassMode,
  setBrowserElevated,
  useApprovals,
} from '@/services/approval-monitor'
import { routerFxLoadPref, routerFxSavePref, type RouterFxPref } from './transcript/routerFx'
import { ElevatedPill } from './ElevatedPill'
import {
  effectiveElevatedMode,
  findSessionUsage,
  normalizeSessionUsage,
  type SessionUsage,
} from './logic'

// config.get carries the router feature state + the global permission default.
interface ConfigGetResponse {
  agentos_router?: { enabled?: boolean; rollout_phase?: string }
  permissions?: { default_mode?: string }
}

// usage.status carries per-session token totals (chat.js:603-604).
interface UsageStatusResponse {
  sessions?: import('./logic').UsageRow[]
}

function tokens(n: number): string {
  return n.toLocaleString()
}

// The composer settings toolbar (chat.js:1256-1280 markup + 1361-1441 bindings).
// Owns the execution-mode pill, the Pilot Router toggle, and a per-session usage
// readout. The elevated-mode storage + store are SHARED with the approvals view
// (services/approval-monitor.ts) — this component reads the session override off
// the same reactive store and persists through the same setBrowserElevated, so
// there is exactly one elevated-mode source of truth.
export function Toolbar({
  sessionKey,
  routerFxEnabled,
  onRouterFxToggle,
}: {
  sessionKey: string
  // The LIVE `_routerFx.enabled` from the stream controller, used to reflect the
  // toggle on mount (chat.js:1484-1485). Optional so the component renders
  // standalone (tests) — then the initial state is hydrated from localStorage.
  routerFxEnabled?: boolean
  // The controller's `setRouterFxEnabled` — mutates the live pref object the
  // engine reads AND persists it (chat.js:1425-1426). When omitted (tests /
  // no controller), the toggle persists locally via `routerFxSavePref`, matching
  // legacy `_routerFxSavePref` exactly.
  onRouterFxToggle?: (enabled: boolean) => void
}) {
  const rpc = useRpc()
  const queryClient = useQueryClient()

  // ── Elevated mode ─────────────────────────────────────────────────────────
  // The SESSION override lives in the shared reactive store; the GLOBAL default
  // comes from config.get. `unavailable` latches after a 403 from the owner-only
  // endpoint (chat.js:2285-2302).
  const sessionMode = useApprovals((s) => s.elevatedMode)
  const [unavailable, setUnavailable] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)

  // config.get — router state + global permission default (chat.js:1470-1490).
  const configQuery = useQuery<ConfigGetResponse>({
    queryKey: ['config.get', 'chat-toolbar'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<ConfigGetResponse>('config.get')
    },
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  })
  const globalMode = configQuery.data?.permissions?.default_mode || ''

  // chat.js:2277-2312 (_syncElevatedMode) — POST the new mode to the owner-only
  // endpoint. A 403 latches `unavailable`, clears the shared elevated mode, and
  // toasts once. Any other failure toasts the error.
  const syncElevatedMode = useCallback(
    async (mode: string) => {
      if (!sessionKey || unavailable) return
      try {
        const resp = await fetch('/api/elevated-mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sessionKey, mode: mode || 'off' }),
        })
        if (resp.status === 403) {
          // chat.js:2285-2302 — non-owner session: latch disabled, clear cache.
          setUnavailable(true)
          setBrowserElevated('')
          toast.warning('Bypass requires a local owner session (loopback only).', {
            duration: 4000,
          })
          return
        }
        if (!resp.ok) throw new Error('HTTP ' + resp.status)
        // chat.js:2305-2308 — if the POST resolved any pending approvals as a
        // side effect (enabling bypass auto-approves them), re-poll now so the
        // badge/modal refresh immediately instead of waiting for the next tick.
        const payload = await resp.json().catch(() => ({}))
        if (payload?.resolvedPending) {
          void approvalMonitor.pollNow()
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        toast.error('Failed to sync bypass mode: ' + message, { duration: 3500 })
      }
    },
    [sessionKey, unavailable],
  )

  // chat.js:2246-2275 (_setElevatedMode) — persist through the shared store
  // (localStorage version 2 + reactive slice), then sync to the endpoint. The
  // outcome toast mirrors the legacy warn-on-set / info-on-clear copy.
  const applyElevatedMode = useCallback(
    (mode: string) => {
      const normalized = setBrowserElevated(mode)
      if (normalized) {
        toast.warning(`Session permission mode: ${normalized}`, { duration: 2500 })
      } else if (globalMode) {
        toast.info(`Session override cleared; global mode: ${globalMode}`, { duration: 2500 })
      } else {
        toast.info('Session permission override cleared', { duration: 2500 })
      }
      void syncElevatedMode(normalized)
    },
    [globalMode, syncElevatedMode],
  )

  // chat.js:1362-1383 (_bindToolbarPills) — an active session override clears on
  // click; otherwise a destructive confirm gates enabling bypass. When latched
  // unavailable, clicking just re-toasts the reason.
  const onPillToggle = useCallback(() => {
    if (unavailable) {
      toast.warning('Bypass requires a local owner session (loopback only).', { duration: 4000 })
      return
    }
    if (sessionMode) {
      applyElevatedMode('')
      return
    }
    setConfirmOpen(true)
  }, [unavailable, sessionMode, applyElevatedMode])

  // ── Pilot Router toggle ───────────────────────────────────────────────────
  // chat.js:1474-1477 — the switch is checked only when enabled AND phase=full.
  const routerEnabled =
    !!configQuery.data?.agentos_router?.enabled &&
    configQuery.data?.agentos_router?.rollout_phase === 'full'
  // Optimistic mirror so the switch flips immediately, reverting on failure.
  const [routerPending, setRouterPending] = useState<boolean | null>(null)
  const routerChecked = routerPending ?? routerEnabled

  // chat.js:1395-1417 — patch agentos_router.enabled + rollout_phase, revert on
  // failure.
  const onRouterToggle = useCallback(
    async (next: boolean) => {
      setRouterPending(next)
      try {
        await rpc.call('config.patch.safe', {
          patches: {
            'agentos_router.enabled': next,
            'agentos_router.rollout_phase': next ? 'full' : 'observe',
          },
        })
        toast.info('Pilot Router: ' + (next ? 'ON' : 'OFF'))
        // Both Toolbar and the imperative transcript keep a config.get query.
        // Invalidate the shared prefix so history strips rebuild against the
        // new operator state instead of leaving the transcript registry stale.
        await queryClient.invalidateQueries({ queryKey: ['config.get'] })
        setRouterPending(null)
      } catch (err) {
        setRouterPending(null)
        const message = err instanceof Error ? err.message : String(err)
        toast.error('Failed: ' + message)
      }
    },
    [rpc, queryClient],
  )

  // ── Visual effects (router-fx) toggle ─────────────────────────────────────
  // chat.js:1272-1278 markup + chat.js:1422-1439 handler + 1483-1485 hydrate.
  // Reflects the router-fx engine's `_routerFx.enabled` (key `agentos-router-fx`,
  // shape {enabled}). Initial state: the live pref from the controller when
  // threaded in, else hydrated from localStorage (defaults ON like legacy).
  const [routerFxChecked, setRouterFxChecked] = useState<boolean>(() => {
    if (typeof routerFxEnabled === 'boolean') return routerFxEnabled
    const pref: RouterFxPref = { enabled: true, variant: 'default' }
    routerFxLoadPref(pref)
    return pref.enabled
  })

  // chat.js:1483-1486 — a focus/config refresh re-hydrates the browser-local
  // preference and updates the mounted switch without remounting the toolbar.
  useEffect(() => {
    if (typeof routerFxEnabled !== 'boolean') return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- controlled external preference synchronization
    setRouterFxChecked(routerFxEnabled)
  }, [routerFxEnabled])

  const onRouterFxChange = useCallback(
    (next: boolean) => {
      setRouterFxChecked(next)
      if (onRouterFxToggle) {
        // The controller mutates its live pref + persists (chat.js:1425-1426).
        onRouterFxToggle(next)
      } else {
        // Standalone: persist the exact `{enabled}` shape (chat.js:3411-3416).
        routerFxSavePref({ enabled: next, variant: 'default' })
      }
      // chat.js:1437 — user-visible confirmation.
      toast.info('Visual effects: ' + (next ? 'ON' : 'OFF'))
    },
    [onRouterFxToggle],
  )

  // ── Usage readout ─────────────────────────────────────────────────────────
  // chat.js:599-625 (_loadCurrentSessionUsage) — the per-session token totals.
  const usageQuery = useQuery<SessionUsage | null>({
    queryKey: ['usage.status', sessionKey],
    queryFn: async () => {
      await rpc.waitForConnection()
      const usage = await rpc.call<UsageStatusResponse>('usage.status', { sessionKey })
      const row = findSessionUsage(usage?.sessions, sessionKey)
      return row ? normalizeSessionUsage(row) : null
    },
    enabled: !!sessionKey,
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  })
  const usage = usageQuery.data ?? null

  // Effective mode drives the pill glow (chat.js:2260 `_toolbarState.bypass`).
  const bypass = isApprovalBypassMode(effectiveElevatedMode(sessionMode, globalMode))

  return (
    <div className="chat-toolbar" data-bypass={bypass ? 'on' : undefined}>
      <div className="chat-toolbar-controls">
        <div className="chat-toolbar-row chat-toolbar-row--mode">
          <span className="chat-toolbar-row-label t-label">Execution mode</span>
          <ElevatedPill
            sessionMode={sessionMode}
            globalMode={globalMode}
            unavailable={unavailable}
            onToggle={onPillToggle}
          />
        </div>

        <div className="chat-toolbar-row">
          <span className="chat-toolbar-row-label t-label">Pilot Router</span>
          <label className="chat-toggle" aria-label="Pilot Router">
            <input
              type="checkbox"
              checked={routerChecked}
              onChange={(e) => void onRouterToggle(e.target.checked)}
            />
            <span className="chat-toggle-track" aria-hidden="true">
              <span className="chat-toggle-thumb" />
            </span>
          </label>
        </div>

        <div className="chat-toolbar-row">
          <span className="chat-toolbar-row-label t-label">Visual effects</span>
          <label className="chat-toggle" aria-label="Visual effects">
            <input
              type="checkbox"
              checked={routerFxChecked}
              onChange={(e) => onRouterFxChange(e.target.checked)}
            />
            <span className="chat-toggle-track" aria-hidden="true">
              <span className="chat-toggle-thumb" />
            </span>
          </label>
        </div>
      </div>

      <div className="chat-toolbar-usage" role="group" aria-labelledby="chat-toolbar-usage-title">
        <div id="chat-toolbar-usage-title" className="chat-toolbar-usage-title t-label">
          Session usage
        </div>
        {usage ? (
          <>
            <div className="chat-toolbar-usage-model">
              <span className="t-label">Model</span>
              <span
                className="chat-toolbar-usage-model-value t-data"
                title={usage.model || 'Not reported'}
              >
                {usage.model || 'Not reported'}
              </span>
            </div>
            <div className="chat-toolbar-usage-metrics" data-has-cost={usage.cost != null}>
              <span className="chat-toolbar-usage-metric">
                <span className="t-label">in</span>
                <span className="t-data">{tokens(usage.input)}</span>
              </span>
              <span className="chat-toolbar-usage-metric">
                <span className="t-label">out</span>
                <span className="t-data">{tokens(usage.output)}</span>
              </span>
              {usage.cost != null ? (
                <span className="chat-toolbar-usage-metric">
                  <span className="t-label">cost</span>
                  <span className="t-data">${usage.cost.toFixed(4)}</span>
                </span>
              ) : null}
            </div>
          </>
        ) : (
          <span className="chat-toolbar-usage-empty t-label">No usage yet</span>
        )}
      </div>

      <AnimatePresence>
        {confirmOpen ? (
          <ModalShell
            role="alertdialog"
            labelledBy="chat-bypass-confirm-title"
            describedBy="chat-bypass-confirm-body"
            overlayClassName="chat-modal-overlay"
            className="chat-modal"
            onClose={() => setConfirmOpen(false)}
          >
            <h2 id="chat-bypass-confirm-title" className="t-display">
              Enable approval bypass?
            </h2>
            <div id="chat-bypass-confirm-body" className="chat-modal-body">
              <p>
                This allows host execution without approval prompts in this browser session. This
                maps to /elevated bypass.
              </p>
              <p>Sensitive-path checks remain active.</p>
            </div>
            <div className="chat-modal-actions">
              <Button type="button" variant="outline" onClick={() => setConfirmOpen(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                onClick={() => {
                  setConfirmOpen(false)
                  applyElevatedMode('bypass')
                }}
              >
                Enable bypass
              </Button>
            </div>
          </ModalShell>
        ) : null}
      </AnimatePresence>
    </div>
  )
}
