import './ApprovalPrompt.css'
import { useState } from 'react'
import { create } from 'zustand'
import { Button } from '@/components/ui/button'
import { CommandLine } from '@/components/CommandLine'
import { DisabledReason } from '@/components/DisabledReason'
import { ModalShell } from '@/components/ModalShell'
import { t, tPlural } from '@/i18n'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { useConnection } from '@/stores/connection'
import {
  approvalCommand,
  approvalDetail,
  approvalMeta,
  approvalMonitor,
  canAlwaysAllow,
  canBypass,
  useApprovals,
  type Approval,
} from '@/services/approval-monitor'

/**
 * Approvals the operator chose to answer later. While every pending approval
 * is in here the modal steps aside for a small dock (no backdrop, no focus
 * trap, shortcuts work), so the rest of the console, the status bar's
 * recovery action and the attention inbox stay reachable. A new approval
 * brings the modal back; an empty queue forgets the list.
 */
const useAnswerLater = create<{ ids: readonly string[] }>(() => ({ ids: [] }))
useApprovals.subscribe((state) => {
  if (state.pending.length === 0 && useAnswerLater.getState().ids.length > 0) {
    useAnswerLater.setState({ ids: [] })
  }
})

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
export function ApprovalPrompt({ onOpenInbox }: { onOpenInbox?: () => void } = {}) {
  const pending = useApprovals((s) => s.pending)
  const mode = useApprovals((s) => s.mode)
  const [busy, setBusy] = useState(false)
  // An answer is delivered through the gateway: while it is unreachable the
  // buttons stay, disabled, with the reason (disabled-with-reason).
  const rpcState = useConnection((s) => s.state)
  const answer = gate(requireConnection(rpcState))
  const later = useAnswerLater((s) => s.ids)

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

  if (!item) return null

  if (pending.every((p) => later.includes(p.id))) {
    return (
      <div
        className="approval-dock tone-warn"
        role="region"
        aria-label={t('shell.approvalDockLandmark')}
        data-testid="approval-dock"
      >
        <span className="approval-dock__light" aria-hidden="true" />
        <span className="approval-dock__text">{tPlural('shell.approvalDock', pending.length)}</span>
        <Button type="button" size="xs" onClick={() => useAnswerLater.setState({ ids: [] })}>
          {t('shell.approvalDockAnswer')}
        </Button>
        {onOpenInbox ? (
          <Button type="button" size="xs" variant="ghost" onClick={onOpenInbox}>
            {t('shell.approvalDockInbox')}
          </Button>
        ) : null}
      </div>
    )
  }

  const command = approvalCommand(item)
  const detail = approvalDetail(item)
  const meta = approvalMeta(item, item.mode || mode)
  const showAlways = canAlwaysAllow(item)
  const showBypass = canBypass(item)
  const toolLabel = item.toolName || item.actionKind || t('shell.approvalFallbackTool')

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
    <ModalShell
      role="alertdialog"
      labelledBy="approval-modal-title"
      onClose={() => undefined}
      dismissible={false}
      overlayClassName="approval-backdrop"
      className="approval-modal panel tone-warn"
    >
      <div className="panel__head">
        <span id="approval-modal-title">{t('shell.approvalTitle')}</span>
      </div>
      <div className="panel__body approval-modal__body">
        <div className="approval-modal__tool">{toolLabel}</div>
        {meta ? <div className="approval-modal__meta t-data">{meta}</div> : null}
        {command ? <CommandLine command={command} toastIdPrefix="approval-copy" /> : null}
        {detail ? <pre className="approval-modal__detail">{detail}</pre> : null}
      </div>
      <div className="approval-modal__foot">
        <DisabledReason gate={answer} className="approval-modal__answers">
          <Button
            type="button"
            disabled={busy || answer.disabled}
            title={t('shell.approvalOnceTitle')}
            onClick={() => void resolve('once')}
          >
            {t('shell.approvalOnce')}
          </Button>
          {showAlways ? (
            <Button
              type="button"
              variant="outline"
              disabled={busy || answer.disabled}
              title={t('shell.approvalAlwaysTitle')}
              onClick={() => void resolve('always')}
            >
              {t('shell.approvalAlways')}
            </Button>
          ) : null}
          {showBypass ? (
            <Button
              type="button"
              variant="outline"
              disabled={busy || answer.disabled}
              title={t('shell.approvalBypassTitle')}
              onClick={() => void resolve('bypass')}
            >
              {t('shell.approvalBypass')}
            </Button>
          ) : null}
          <Button
            type="button"
            variant="destructive"
            disabled={busy || answer.disabled}
            onClick={() => void resolve('deny')}
          >
            {t('shell.approvalDeny')}
          </Button>
        </DisabledReason>
        {/* Always usable: stepping aside never answers anything. */}
        <Button
          type="button"
          variant="ghost"
          className="approval-modal__later"
          title={t('shell.approvalLaterTitle')}
          onClick={() => useAnswerLater.setState({ ids: pending.map((p) => p.id) })}
        >
          {t('shell.approvalLater')}
        </Button>
      </div>
    </ModalShell>
  )
}
