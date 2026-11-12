import { ShieldAlert } from 'lucide-react'
import { useMemo } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { useApprovals } from '@/services/approval-monitor'
import '@/web/styles/chat-thread.css'
import { ApprovalCard, approvalTargetsSession } from '@/web/shell/ApprovalTray'

/**
 * Pending approvals for the open session, docked above the composer: a warm,
 * deliberate frame that rises into place, not an alarm.
 */
export function ApprovalDock({ sessionKey }: { sessionKey: string }) {
  const pending = useApprovals((s) => s.pending)
  const items = useMemo(
    () => pending.filter((a) => approvalTargetsSession(a, sessionKey)),
    [pending, sessionKey],
  )
  if (items.length === 0) return null
  const title = t('webChat.dockTitle', {
    tool: items[0]!.toolName || items[0]!.namespace || 'exec',
  })
  return (
    <div className="chat-column mb-3">
      <section aria-label={title} className="chat-approval anim-enter">
        <p className="chat-approval-title" aria-hidden>
          <span className="chat-approval-icon">
            <ShieldAlert />
          </span>
          <span className="min-w-0 flex-1 truncate">{title}</span>
          <span className="status-dot motion-status size-1.5" data-live />
        </p>
        <ul role="list" className="flex flex-col gap-1.5">
          {items.map((item) => (
            <ApprovalCard key={item.id} item={item} />
          ))}
        </ul>
      </section>
    </div>
  )
}
