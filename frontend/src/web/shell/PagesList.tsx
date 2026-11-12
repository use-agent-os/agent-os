import { useQuery } from '@tanstack/react-query'
import { Settings } from 'lucide-react'
import { useId, type KeyboardEvent, type ReactNode } from 'react'
import { NavLink } from 'react-router'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webShell'
import { useAttentionInbox } from '@/lib/useAttentionInbox'
import { useGlide } from '@/web/components/ui/use-glide'
import { PAGES, type PageEntry } from './pages'
import { fetchSessions, SESSIONS_QUERY_KEY, SESSIONS_REFRESH_MS } from './session-data'
import { sessionIsRunning } from './session-list'

const GROUPS: {
  id: PageEntry['group']
  titleKey: 'webShell.pagesGroupOverview' | 'webShell.pagesGroupWork'
}[] = [
  { id: 'overview', titleKey: 'webShell.pagesGroupOverview' },
  { id: 'work', titleKey: 'webShell.pagesGroupWork' },
]

/**
 * Live figures beside a page, from data the shell already holds: the
 * sessions list cache (read, not polled: the Sessions view owns the poll) and
 * the attention inbox.
 */
function usePageCounts() {
  const rpc = useRpc()
  const sessions = useQuery({
    queryKey: SESSIONS_QUERY_KEY,
    queryFn: () => fetchSessions(rpc),
    staleTime: SESSIONS_REFRESH_MS,
  })
  const inbox = useAttentionInbox().count
  const rows = sessions.data ?? []
  return {
    sessions: sessions.data ? rows.length : null,
    running: rows.filter(sessionIsRunning).length,
    inbox,
  }
}

/** Arrow keys, Home and End move between the pane's links (Tab still works). */
function onListKeyDown(e: KeyboardEvent<HTMLElement>) {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
  const links = [...e.currentTarget.querySelectorAll<HTMLAnchorElement>('a[href]')]
  if (links.length === 0) return
  const at = links.indexOf(document.activeElement as HTMLAnchorElement)
  const next =
    e.key === 'Home'
      ? 0
      : e.key === 'End'
        ? links.length - 1
        : e.key === 'ArrowDown'
          ? (at + 1) % links.length
          : (at - 1 + links.length) % links.length
  e.preventDefault()
  links[next]?.focus()
}

function PageLink({
  to,
  icon,
  label,
  description,
  trailing,
  onNavigate,
}: {
  to: string
  icon: ReactNode
  label: string
  description?: string
  trailing?: ReactNode
  onNavigate?: () => void
}) {
  const id = useId()
  return (
    <>
      <NavLink
        to={to}
        viewTransition
        onClick={onNavigate}
        aria-describedby={description ? id : undefined}
        className="page-link press target"
      >
        <span aria-hidden className="page-link-icon">
          {icon}
        </span>
        <span data-page-label className="min-w-0 flex-1 truncate">
          {label}
        </span>
        {trailing}
      </NavLink>
      {description ? (
        <span id={id} hidden>
          {description}
        </span>
      ) : null}
    </>
  )
}

/**
 * The Pages pane: a compact navigation list in two groups, one gliding pill
 * on the current page, live counts where the shell has them, and Settings at
 * the foot. Descriptions are the links' accessible descriptions (and the
 * command palette's), not a second line of text.
 */
export function PagesList({ onNavigate }: { onNavigate?: () => void }) {
  // One pill glides to the current page across both groups (the track is the
  // links' offset parent; the hook sums offsets through the group lists).
  const track = useGlide<HTMLDivElement>()
  const counts = usePageCounts()

  const extra = (page: PageEntry): { trailing?: ReactNode; said: string[] } => {
    if (page.id === 'sessions' && counts.sessions !== null && counts.sessions > 0) {
      return {
        said:
          counts.running > 0 ? [t('webShell.pagesCountRunning', { count: counts.running })] : [],
        trailing: (
          <span aria-hidden className="page-count">
            {counts.running > 0 ? (
              <span data-live className="status-dot motion-status size-1.5 text-accent" />
            ) : null}
            {counts.sessions}
          </span>
        ),
      }
    }
    if (page.id === 'inbox' && counts.inbox > 0) {
      return {
        said: [tPlural('webShell.attentionAnnounce', counts.inbox)],
        trailing: (
          <span aria-hidden className="page-count" data-tone="attention">
            {counts.inbox > 99 ? '99+' : counts.inbox}
          </span>
        ),
      }
    }
    return { said: [] }
  }

  return (
    <nav
      aria-label={t('webShell.pagesHeading')}
      onKeyDown={onListKeyDown}
      className="flex min-h-0 flex-1 flex-col"
    >
      <div className="shrink-0 px-3 pt-3 pb-2">
        <div className="flex h-7 items-center pl-1">
          <h2 className="text-section">{t('webShell.pagesHeading')}</h2>
        </div>
      </div>
      <div
        ref={track}
        className="glide-track scroll-contained flex min-h-0 flex-1 flex-col gap-4 px-2 pt-1 pb-2 [--glide-bg:var(--surface-3)] [--glide-radius:8px]"
      >
        {GROUPS.map((group) => {
          const headingId = `pages-group-${group.id}`
          return (
            <section key={group.id} aria-labelledby={headingId} className="flex flex-col">
              <h3 id={headingId} className="pane-group-label">
                {t(group.titleKey)}
              </h3>
              <ul role="list" className="flex flex-col gap-px">
                {PAGES.filter((page) => page.group === group.id).map((page) => {
                  const { trailing, said } = extra(page)
                  return (
                    <li key={page.id}>
                      <PageLink
                        to={page.path}
                        icon={<page.icon />}
                        label={t(page.titleKey)}
                        description={[t(page.descriptionKey), ...said].join('. ')}
                        trailing={trailing}
                        onNavigate={onNavigate}
                      />
                    </li>
                  )
                })}
              </ul>
            </section>
          )
        })}
        <div className="mt-auto border-t border-border pt-2">
          <PageLink
            to="/settings"
            icon={<Settings />}
            label={t('webShell.pageSettings')}
            onNavigate={onNavigate}
          />
        </div>
      </div>
    </nav>
  )
}
