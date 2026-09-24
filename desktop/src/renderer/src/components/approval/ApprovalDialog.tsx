import './approval-dialog.css'
import { ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import { ModalShell } from '@/components/ModalShell'
import {
  approvalCommand,
  approvalMonitor,
  canAlwaysAllow,
  useApprovals,
  type Approval,
  type ApprovalAction,
} from '@/services/approval-monitor'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'

/**
 * The desktop's approval prompt. Same queue, same resolve() as the console's
 * ApprovalPrompt (services/approval-monitor is shared logic), but its own
 * markup and CSS: the console's prompt is a `.panel` whose background only
 * exists under the console's `.control-surface` stylesheet, so mounted in the
 * desktop it rendered as bare text and unstyled buttons over the chat.
 *
 * Shaped like an NSAlert: icon, a one-line title naming the tool, the files
 * or command being asked about, and a row of push buttons with the safe
 * choice (Deny) as the default.
 */
export function ApprovalDialog() {
  const pending = useApprovals((s) => s.pending)
  const [busy, setBusy] = useState(false)
  // Pin the shown item so a queue reshuffle never swaps the dialog under the
  // user's cursor (same rule as the console's prompt); advance only when the
  // pinned item leaves the queue. Adjusted during render, per React's
  // "state derived from props with memory" pattern.
  const [item, setItem] = useState<Approval | null>(null)
  const [resolvedId, setResolvedId] = useState<string | null>(null)

  if (resolvedId != null && !pending.some((p) => p.id === resolvedId)) {
    setResolvedId(null)
  }
  const stillPending =
    item != null && item.id !== resolvedId && pending.some((p) => p.id === item.id)
  const nextItem = stillPending ? item : (pending.find((p) => p.id !== resolvedId) ?? null)
  if ((nextItem?.id ?? null) !== (item?.id ?? null)) {
    setItem(nextItem)
  }

  if (!item) return null

  const toolLabel = item.toolName || item.actionKind || t('approval.fallbackTool')
  const paths = targetPaths(item)
  const command = approvalCommand(item)
  // A patch approval's "command" is a content fingerprint: useless to read,
  // so it is left out once the file list carries the meaning.
  const showCommand = command && !(item.mode === 'patch' && paths.length > 0)

  async function resolve(action: ApprovalAction): Promise<void> {
    if (busy || !item) return
    const resolvingId = item.id
    setBusy(true)
    try {
      await approvalMonitor.resolve(item, action)
      setResolvedId(resolvingId)
    } catch {
      // resolve() already toasted; keep the item so the user can retry.
    } finally {
      setBusy(false)
    }
  }

  return (
    <ModalShell
      role="alertdialog"
      labelledBy="approval-dialog-title"
      describedBy="approval-dialog-body"
      onClose={() => undefined}
      dismissible={false}
      overlayClassName="approval-dialog__overlay"
      className="approval-dialog"
    >
      <div className="approval-dialog__icon" aria-hidden="true">
        <ShieldAlert size={28} strokeWidth={1.75} />
      </div>
      <h2 id="approval-dialog-title" className="approval-dialog__title">
        {t('approval.title')} <code>{toolLabel}</code>
      </h2>
      <div id="approval-dialog-body" className="approval-dialog__body">
        {paths.length > 0 ? (
          <>
            <p className="approval-dialog__lead">{t('approval.writes')}</p>
            <ul className="approval-dialog__paths">
              {paths.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </>
        ) : null}
        {showCommand ? <pre className="approval-dialog__command">{command}</pre> : null}
        {item.warning ? <p className="approval-dialog__warning">{item.warning}</p> : null}
        {item.sessionKey ? (
          <p className="approval-dialog__meta">
            {t('approval.session')} {item.sessionKey}
          </p>
        ) : null}
      </div>
      {/* Deny is first in the DOM so the shell's focus-first lands on it (the
          default button); the row is laid out reversed so it still sits on
          the right, where a macOS alert keeps its default. */}
      <div className="approval-dialog__buttons">
        <Button
          variant="primary"
          disabled={busy}
          title={t('approval.deny.title')}
          onClick={() => void resolve('deny')}
        >
          {t('approval.deny')}
        </Button>
        <Button
          disabled={busy}
          title={t('approval.once.title')}
          onClick={() => void resolve('once')}
        >
          {t('approval.once')}
        </Button>
        {canAlwaysAllow(item) ? (
          <Button
            disabled={busy}
            title={t('approval.always.title')}
            onClick={() => void resolve('always')}
          >
            {t('approval.always')}
          </Button>
        ) : null}
        <Button
          variant="ghost"
          disabled={busy}
          title={t('approval.bypass.title')}
          onClick={() => void resolve('bypass')}
        >
          {t('approval.bypass')}
        </Button>
      </div>
    </ModalShell>
  )
}

/** The files an out-of-workspace write wants to touch, when the gate named them. */
function targetPaths(item: Approval): string[] {
  const args = item.args as { outside_paths?: unknown } | null | undefined
  const raw = args?.outside_paths
  if (!Array.isArray(raw)) return []
  return raw.filter((p): p is string => typeof p === 'string' && p.length > 0)
}
