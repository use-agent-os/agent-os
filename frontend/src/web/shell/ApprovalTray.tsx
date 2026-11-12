import { ArrowUpRight, ChevronDown, Copy, ShieldAlert, Terminal } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router'
import { toast } from 'sonner'
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
import { copyWithFallback } from '@/lib/clipboard'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { useDisplayedConnection } from './use-displayed-connection'
import { pathToSessionKey, sessionKeyToPath } from './session-url'
import { usePhone } from './use-media-query'

/** Whether an approval belongs to (or is visible from) the open session. */
export function approvalTargetsSession(item: Approval, sessionKey: string | null): boolean {
  if (!sessionKey) return false
  if (item.sessionKey === sessionKey) return true
  return (item.audienceSessionKeys ?? []).includes(sessionKey)
}

/** A request's kind when it names no tool (an identifier, not copy). */
const EXEC = 'exec'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * One pending approval with its decisions. `highlighted` marks the request a
 * deep link points at (a push notification's `?approval=<id>`): the card is
 * scrolled into view, focused and outlined. `className` adjusts the card's
 * surface for its host (the tray, the inbox, the chat dock).
 */
export function ApprovalCard({
  item,
  highlighted = false,
  className,
}: {
  item: Approval
  highlighted?: boolean
  className?: string
}) {
  const connection = useDisplayedConnection()
  const offline = gate(requireConnection(connection))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLLIElement>(null)
  const command = approvalCommand(item)
  const detail = approvalDetail(item)
  const tool = item.toolName || item.title || item.namespace || EXEC

  useEffect(() => {
    if (!highlighted) return
    ref.current?.scrollIntoView({ block: 'center' })
    ref.current?.focus({ preventScroll: true })
  }, [highlighted])

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

  const copy = () =>
    void copyWithFallback(command)
      .then(() => toast.success(t('webShell.trayCopied')))
      .catch(() => toast.error(t('webShell.trayCopyFailed')))

  return (
    <li
      ref={ref}
      tabIndex={highlighted ? -1 : undefined}
      data-approval-id={item.id}
      data-highlighted={highlighted ? '' : undefined}
      className={cn(
        'flex min-w-0 flex-col gap-2.5 rounded-card border border-border bg-surface-1 p-3 shadow-card',
        highlighted && 'border-accent ring-2 ring-accent',
        className,
      )}
    >
      <div className="flex items-start gap-2.5">
        <span
          aria-hidden
          className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md border border-border bg-surface-2 text-text-muted [&_svg]:size-3.5"
        >
          <Terminal />
        </span>
        {/* w-0: the truncated lines must not set the card's min-content width,
            or a narrow host (the inbox at 360 px) is pushed off screen. */}
        <div className="w-0 min-w-0 flex-1">
          <p className="truncate font-mono text-[13px] font-semibold tracking-tight">{tool}</p>
          {item.agent ? (
            <p className="truncate text-small text-text-muted">
              {t('webShell.trayAgent', { agent: item.agent })}
            </p>
          ) : null}
          {item.sessionKey ? (
            <p className="truncate text-small text-text-muted">
              {t('webShell.traySession', { session: item.sessionKey })}
            </p>
          ) : null}
        </div>
        {item.sessionKey ? (
          <Link
            to={sessionKeyToPath(item.sessionKey)}
            viewTransition
            className="target -mt-0.5 -mr-1 inline-flex h-7 shrink-0 items-center gap-0.5 rounded-md px-1.5 text-small font-medium text-accent underline-offset-2 hover:underline"
          >
            {t('webShell.trayOpenSession')}
            <ArrowUpRight className="size-3.5" aria-hidden />
          </Link>
        ) : null}
      </div>
      {command || detail ? (
        <div className="group/cmd relative">
          <pre
            tabIndex={0}
            className={`scroll-contained max-h-28 rounded-md border border-border bg-surface-inset py-2 pl-2.5 text-small whitespace-pre-wrap shadow-inset [overflow-wrap:anywhere] ${command ? 'pr-9' : 'pr-2.5'}`}
          >
            {command || detail}
          </pre>
          {command ? (
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={t('webShell.trayCopyCommand')}
              title={t('webShell.trayCopyCommand')}
              onClick={copy}
              className="absolute top-1 right-1 text-text-muted hover:text-text"
            >
              <Copy />
            </Button>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="anim-fade-in text-small text-danger">
          {error}
        </p>
      ) : null}
      {offline.reason ? <p className="text-small text-text-muted">{offline.reason}</p> : null}
      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant="primary"
          disabled={busy || offline.disabled}
          onClick={() => void decide('once')}
        >
          {t('webShell.trayAllowOnce')}
        </Button>
        {canAlwaysAllow(item) ? (
          <Button
            size="sm"
            variant="secondary"
            disabled={busy || offline.disabled}
            onClick={() => void decide('always')}
          >
            {t('webShell.trayAlwaysAllow')}
          </Button>
        ) : null}
        {canBypass(item) ? (
          <Button
            size="sm"
            variant="secondary"
            disabled={busy || offline.disabled}
            onClick={() => void decide('bypass')}
          >
            {t('webShell.trayBypass')}
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="outline"
          disabled={busy || offline.disabled}
          onClick={() => void decide('deny')}
          className="ml-auto border-danger/50 text-danger hover:border-danger hover:bg-danger/10"
        >
          {t('webShell.trayDeny')}
        </Button>
      </div>
    </li>
  )
}

/**
 * Publishes the tray's height as `--tray-stack` on the root while it is shown,
 * so toasts stack above it rather than on it (shell.css "Floating layers").
 * Module-level so React attaches it once, not on every render.
 */
function publishTrayStack(el: HTMLElement | null) {
  if (!el || typeof ResizeObserver === 'undefined') return
  const root = document.documentElement.style
  const observer = new ResizeObserver(() =>
    root.setProperty('--tray-stack', `${el.offsetHeight}px`),
  )
  observer.observe(el)
  return () => {
    observer.disconnect()
    root.removeProperty('--tray-stack')
  }
}

/**
 * Marks which edges of a scroller hide content (`data-scroll-fade` = `start`,
 * `end` or `both`), so the stylesheet can fade them: a tray body cut off by
 * its capped height otherwise reads as a card chopped at the bottom.
 */
function trackScrollEdges(el: HTMLElement | null) {
  if (!el) return
  const update = () => {
    const before = el.scrollTop > 1
    const after = el.scrollHeight - el.clientHeight - el.scrollTop > 1
    const edge = before && after ? 'both' : before ? 'start' : after ? 'end' : null
    if (edge) el.dataset.scrollFade = edge
    else delete el.dataset.scrollFade
  }
  update()
  el.addEventListener('scroll', update, { passive: true })
  // The body's own box stops changing once it is capped, so watch the cards
  // too (an error line, a card added or resolved).
  const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update)
  const watch = () => {
    resize?.disconnect()
    resize?.observe(el)
    for (const child of el.children) resize?.observe(child)
  }
  watch()
  const mutations = new MutationObserver(watch)
  mutations.observe(el, { childList: true })
  return () => {
    el.removeEventListener('scroll', update)
    resize?.disconnect()
    mutations.disconnect()
  }
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
  // It starts as a compact pill on phones, and on chat pages at any width:
  // there the open session has its own approval dock, and the expanded tray
  // sat over the newest lines of the transcript. Once the user expands or
  // collapses it, that choice holds across pages. It is never dismissed
  // while a request waits.
  const phone = usePhone()
  const [choice, setChoice] = useState<boolean | null>(null)
  const collapsed = choice ?? (phone || pathname.startsWith('/chat'))
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
  const first = items[0]!
  // The same fallback the card uses for a request without a tool name.
  const firstTool = first.toolName || first.title || first.namespace || EXEC
  return (
    <aside
      ref={publishTrayStack}
      aria-label={t('webShell.trayLabel')}
      data-collapsed={collapsed || undefined}
      className={cn(
        'approval-tray fixed z-[calc(var(--z-drawer)-1)] flex flex-col overflow-hidden border border-transparent bg-surface-overlay [view-transition-name:agentos-tray]',
        collapsed
          ? 'w-auto max-w-[calc(100vw-1rem)] rounded-full'
          : 'w-[min(calc(100vw-1rem),400px)] rounded-overlay max-sm:w-auto',
      )}
    >
      <span role="status" aria-live="polite" className="sr-only">
        {announcement}
      </span>
      <div
        className={cn(
          'flex shrink-0 items-center gap-2.5 bg-[linear-gradient(180deg,color-mix(in_oklab,var(--warn)_10%,transparent),transparent)]',
          collapsed ? 'py-1.5 pr-1.5 pl-2' : 'border-b border-border py-2.5 pr-2 pl-3',
        )}
      >
        <span
          aria-hidden
          className="relative grid size-7 shrink-0 place-items-center rounded-full bg-warn/15 text-warn"
        >
          <ShieldAlert className="size-4" data-status-icon />
          <span className="absolute -top-0.5 -right-0.5 flex rounded-full bg-surface-overlay p-0.5">
            <span data-live className="status-dot motion-status size-1.5" />
          </span>
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold tabular-nums">{title}</h2>
          {collapsed ? null : (
            <p className="truncate text-small text-text-muted">{t('webShell.traySubtitle')}</p>
          )}
        </div>
        {collapsed ? (
          <span className="max-w-40 truncate rounded-full border border-border bg-surface-2 px-2 font-mono text-small leading-5 text-text-muted max-sm:hidden">
            {firstTool}
          </span>
        ) : null}
        <Button
          size="icon-sm"
          variant="ghost"
          aria-expanded={!collapsed}
          aria-label={collapsed ? t('webShell.trayExpand') : t('webShell.trayCollapse')}
          onClick={() => setChoice(!collapsed)}
          className={cn('text-text-muted hover:text-text', collapsed && 'rounded-full')}
        >
          <ChevronDown
            className={cn(
              'transition-transform duration-[var(--duration-base)] ease-[var(--ease-out)]',
              collapsed && 'rotate-180',
            )}
          />
        </Button>
      </div>
      {collapsed ? null : (
        <ul
          ref={trackScrollEdges}
          role="list"
          className="tray-body anim-fade-in scroll-contained flex min-h-0 flex-col gap-2 p-2.5"
        >
          {items.map((item) => (
            <ApprovalCard key={item.id} item={item} className="anim-enter bg-surface-2" />
          ))}
        </ul>
      )}
    </aside>
  )
}
