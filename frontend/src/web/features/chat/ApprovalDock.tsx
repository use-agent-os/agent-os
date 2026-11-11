import { ShieldAlert } from 'lucide-react'
import { useMemo } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { useApprovals } from '@/services/approval-monitor'
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
      <section aria-label={title} className="chat-approval anim-enter p-1.5">
        <p
          className="flex items-center gap-2 px-2 pt-0.5 pb-1.5 text-small font-medium text-warn"
          aria-hidden
        >
          <span className="status-dot motion-status size-1.5" data-live />
          <ShieldAlert className="size-3.5" />
          {title}
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
