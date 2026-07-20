import './ApprovalPrompt.css'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { CommandLine } from '@/components/CommandLine'
import {
  approvalCommand,
  approvalDetail,
  approvalMeta,
  approvalMonitor,
  canAlwaysAllow,
  useApprovals,
  type Approval,
} from '@/services/approval-monitor'

/**
 * Global approval prompt modal — the React port of approval_monitor.js's
 * imperative _openModal/_resolve (lines 140-220). It renders the FIRST pending
 * approval from the useApprovals store as a blocking dialog and drives
 * resolution through approvalMonitor.resolve(). One instance is mounted in
 * AppShell; it renders nothing when the queue is empty.
 *
 * Button → action map (approval_monitor.js:164-179):
 *   Approve This Time  → 'once'   (approved)
 *   Always Allow …     → 'always' (approved + allowAlways + rememberIntent) — exec-only
 *   Bypass Approvals   → 'bypass' (approved + elevatedMode:'bypass')
 *   Deny               → 'deny'   (approved:false)
 *
 * Design system: a warn-toned panel (the pending approval is an attention
 * state) on a dimmed backdrop; the command renders through the common
 * <CommandLine>. Status color flows only through the `--tone` primitive.
 */
export function ApprovalPrompt() {
  const pending = useApprovals((s) => s.pending)
  const mode = useApprovals((s) => s.mode)
  const [busy, setBusy] = useState(false)
  const dialogRef = useRef<HTMLDivElement | null>(null)

  // approval_monitor.js:90-91 — legacy PINNED the modal to the item captured at
  // open time and short-circuited polls while the modal was open (`if (_modal)
  // return`), so the displayed approval never silently swapped under the
  // operator. React re-renders on every store change; we reproduce the pin by
  // holding the shown item in state and, when it drifts out of sync with the
  // queue, ADJUSTING STATE DURING RENDER (the React-sanctioned pattern for
  // "state derived from props with memory" — https://react.dev/reference/react/useState#storing-information-from-previous-renders).
  // Once open, a changing queue head (a new higher-priority approval, or the
  // tail shifting) leaves the shown item untouched; only when the pinned item
  // leaves the queue (resolved here or elsewhere) do we advance to the new head,
  // mirroring legacy reopening for pending[0] after _closeModal.
  const [item, setItem] = useState<Approval | null>(null)
  // Set in the resolve handler on SUCCESS to release the pin before the re-poll
  // drains `pending` (finding 3): the head is skipped while it lingers in the
  // queue, so the resolved item never flashes. Cleared (during render) once it
  // has drained.
  const [resolvedId, setResolvedId] = useState<string | null>(null)

  const resolvedStillQueued = resolvedId != null && pending.some((p) => p.id === resolvedId)
  if (resolvedId != null && !resolvedStillQueued) {
    // The just-resolved item has drained from the queue; drop the skip marker.
    setResolvedId(null)
  }

  const stillPending =
    item != null && item.id !== resolvedId && pending.some((p) => p.id === item.id)
  // Advance to the head, skipping a just-resolved item still lingering at the head.
  const nextItem = stillPending ? item : (pending.find((p) => p.id !== resolvedId) ?? null)
  // Re-pin only when the identity actually changed (guards the render-phase
  // setState against an infinite loop).
  if ((nextItem?.id ?? null) !== (item?.id ?? null)) {
    setItem(nextItem)
  }

  // Move focus into the dialog when the pinned item changes (open / advance) so
  // keyboard users land inside it.
  useEffect(() => {
    if (!item) return
    dialogRef.current?.focus()
  }, [item])

  if (!item) return null

  const command = approvalCommand(item)
  const detail = approvalDetail(item)
  const meta = approvalMeta(item, item.mode || mode)
  const showAlways = canAlwaysAllow(item)
  const toolLabel = item.toolName || item.actionKind || 'Tool execution'

  async function resolve(action: 'once' | 'always' | 'bypass' | 'deny'): Promise<void> {
    if (busy || !item) return
    const resolvingId = item.id
    setBusy(true)
    try {
      await approvalMonitor.resolve(item, action)
      // approval_monitor.js:206 — legacy _closeModal() ran on resolve success
      // BEFORE the 150ms re-poll. Mark the item resolved so the pin releases
      // immediately (the dialog closes / advances to the next head) rather than
      // waiting for the store re-poll to drain `pending`; the resolved item can
      // never flash while the re-poll is in flight.
      setResolvedId(resolvingId)
    } catch {
      // resolve() already toasted the failure; re-enable the buttons so the
      // operator can retry (approval_monitor.js:214-216). The pinned item is
      // kept so the operator can retry the SAME approval.
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="approval-backdrop" role="presentation">
      <div
        ref={dialogRef}
        className="approval-modal panel tone-warn"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="approval-modal-title"
        tabIndex={-1}
      >
        <div className="panel__head">
          <span id="approval-modal-title">Approval Required</span>
        </div>
        <div className="panel__body approval-modal__body">
          <div className="approval-modal__tool">{toolLabel}</div>
          {meta ? <div className="approval-modal__meta t-data">{meta}</div> : null}
          {command ? <CommandLine command={command} toastIdPrefix="approval-copy" /> : null}
          {detail ? <pre className="approval-modal__detail">{detail}</pre> : null}
        </div>
        <div className="approval-modal__foot">
          <Button
            type="button"
            disabled={busy}
            title="Approve only this pending tool call"
            onClick={() => void resolve('once')}
          >
            Approve This Time
          </Button>
          {showAlways ? (
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              title="Remember this operation type for future matching intents"
              onClick={() => void resolve('always')}
            >
              Always Allow This Type
            </Button>
          ) : null}
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            title="Enable approval bypass in this browser session and approve this pending tool call"
            onClick={() => void resolve('bypass')}
          >
            Bypass Approvals
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={busy}
            onClick={() => void resolve('deny')}
          >
            Deny
          </Button>
        </div>
      </div>
    </div>
  )
}
