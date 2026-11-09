import { useMemo } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { useApprovals } from '@/services/approval-monitor'
import { ApprovalCard, approvalTargetsSession } from '@/web/shell/ApprovalTray'

/** Pending approvals for the open session, docked above the composer. */
export function ApprovalDock({ sessionKey }: { sessionKey: string }) {
  const pending = useApprovals((s) => s.pending)
  const items = useMemo(
    () => pending.filter((a) => approvalTargetsSession(a, sessionKey)),
    [pending, sessionKey],
  )
  if (items.length === 0) return null
  return (
    <section
      aria-label={t('webChat.dockTitle', {
        tool: items[0]!.toolName || items[0]!.namespace || 'exec',
      })}
      className="mx-auto mb-2 w-full max-w-3xl px-4"
    >
      <ul role="list" className="flex flex-col gap-2">
        {items.map((item) => (
          <ApprovalCard key={item.id} item={item} />
        ))}
      </ul>
    </section>
  )
}
