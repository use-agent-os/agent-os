import { ChevronDown, ChevronUp, ShieldAlert } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webShell'
import {
  approvalCommand,
  approvalDetail,
  approvalMonitor,
  canAlwaysAllow,
  canBypass,
  useApprovals,
  type Approval,
  type ApprovalAction,
} from '@/services/approval-monitor'
import { Button } from '@/web/components/ui/button'
import { pathToSessionKey, sessionKeyToPath } from './session-url'

/** Whether an approval belongs to (or is visible from) the open session. */
export function approvalTargetsSession(item: Approval, sessionKey: string | null): boolean {
  if (!sessionKey) return false
  if (item.sessionKey === sessionKey) return true
  return (item.audienceSessionKeys ?? []).includes(sessionKey)
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function ApprovalCard({ item }: { item: Approval }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const command = approvalCommand(item)
  const detail = approvalDetail(item)
  const tool = item.toolName || item.title || item.namespace || 'exec'

  const decide = async (action: ApprovalAction) => {
    setBusy(true)
    setError(null)
    try {
      await approvalMonitor.resolve(item, action)
    } catch (e) {
      setError(t('webShell.trayFailed', { message: errorMessage(e) }))
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="flex flex-col gap-2 rounded-lg border border-border bg-surface-1 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium">{tool}</p>
          {item.sessionKey ? (
            <p className="truncate text-small text-text-muted">
              {t('webShell.traySession', { session: item.sessionKey })}
            </p>
          ) : null}
        </div>
        {item.sessionKey ? (
          <Link
            to={sessionKeyToPath(item.sessionKey)}
            className="shrink-0 text-small text-accent underline-offset-2 hover:underline"
          >
            {t('webShell.trayOpenSession')}
          </Link>
        ) : null}
      </div>
      {command || detail ? (
        <pre className="scroll-contained max-h-28 rounded bg-surface-0 p-2 text-small whitespace-pre-wrap">
          {command || detail}
        </pre>
      ) : null}
      {error ? (
        <p role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-1.5">
        <Button size="sm" variant="primary" disabled={busy} onClick={() => void decide('once')}>
          {t('webShell.trayAllowOnce')}
        </Button>
        {canAlwaysAllow(item) ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void decide('always')}>
            {t('webShell.trayAlwaysAllow')}
          </Button>
        ) : null}
        {canBypass(item) ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void decide('bypass')}>
            {t('webShell.trayBypass')}
          </Button>
        ) : null}
        <Button size="sm" variant="danger" disabled={busy} onClick={() => void decide('deny')}>
          {t('webShell.trayDeny')}
        </Button>
      </div>
    </li>
  )
}

/**
 * The non-modal approval tray (shell spec "Approval tray"): every pending
 * request whose session is not the open one. It may collapse to a count but
 * never disappears while a request is pending.
 */
export function ApprovalTray() {
  const pending = useApprovals((s) => s.pending)
  const { pathname } = useLocation()
  const openKey = pathname.startsWith('/chat/') ? pathToSessionKey(pathname) : null
  const items = useMemo(
    () => pending.filter((item) => !approvalTargetsSession(item, openKey)),
    [pending, openKey],
  )
  const [collapsed, setCollapsed] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const seen = useRef(new Set<string>())

  useEffect(() => {
    const fresh = items.filter((item) => !seen.current.has(item.id))
    for (const item of items) seen.current.add(item.id)
    const first = fresh[0]
    if (first) {
      setAnnouncement(
        t('webShell.trayAnnounce', { tool: first.toolName || first.namespace || 'exec' }),
      )
    }
  }, [items])

  if (items.length === 0) {
    return (
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
    )
  }

  const title = tPlural('webShell.trayTitle', items.length)
  return (
    <aside
      aria-label={t('webShell.trayLabel')}
      className="fixed right-4 bottom-4 z-[var(--z-popover)] w-[min(92vw,380px)] rounded-xl border border-warn/60 bg-surface-2 shadow-3"
    >
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
      <div className="flex items-center gap-2 px-3 py-2">
        <ShieldAlert className="size-4 text-warn" aria-hidden data-status-icon />
        <h2 className="flex-1 font-semibold">{title}</h2>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-expanded={!collapsed}
          aria-label={collapsed ? t('webShell.trayExpand') : t('webShell.trayCollapse')}
          onClick={() => setCollapsed((c) => !c)}
        >
          {collapsed ? <ChevronUp /> : <ChevronDown />}
        </Button>
      </div>
      {collapsed ? null : (
        <ul role="list" className="scroll-contained flex max-h-[60dvh] flex-col gap-2 px-3 pb-3">
          {items.map((item) => (
            <ApprovalCard key={item.id} item={item} />
          ))}
        </ul>
      )}
    </aside>
  )
}
