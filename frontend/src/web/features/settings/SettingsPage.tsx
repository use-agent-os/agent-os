import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, Loader2, RefreshCw, RotateCw, Search } from 'lucide-react'
import { lazy, Suspense, useEffect, useState, type ComponentType } from 'react'
import { Link, NavLink, useLocation, useNavigate, useParams } from 'react-router'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webSettings'
import '@/i18n/en/webShell'
import { cn } from '@/lib/utils'
import {
  loadSettingsSnapshot,
  readinessFromSnapshot,
  SETTINGS_SNAPSHOT_QUERY_KEY,
} from '@/views/settings/snapshot'
import { Button, buttonVariants } from '@/web/components/ui/button'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useRestartNotice } from './config-editor'
import { useSettingsFields } from './fields'
import { findSection, SETTINGS_GROUPS, SETTINGS_SECTIONS } from './registry'
import { searchSettings } from './search'

function section(load: () => Promise<Record<string, unknown>>, name: string) {
  return lazy(async () => ({ default: (await load())[name] as ComponentType }))
}

/** Section components, each in its own chunk. */
export const SECTION_COMPONENTS: Record<string, ComponentType> = {
  appearance: section(() => import('./sections/general'), 'AppearanceSection'),
  notifications: section(() => import('./sections/general'), 'NotificationsSection'),
  gateway: section(() => import('./sections/gateway'), 'GatewaySection'),
  channels: section(() => import('./sections/channels'), 'ChannelsSection'),
  agents: section(() => import('./sections/agents'), 'AgentsSection'),
  models: section(() => import('./sections/config-sections'), 'ModelsSection'),
  search: section(() => import('./sections/config-sections'), 'SearchSection'),
  media: section(() => import('./sections/config-sections'), 'MediaSection'),
  skills: section(() => import('./sections/skills'), 'SkillsSection'),
  mcp: section(() => import('./sections/mcp'), 'McpSection'),
  memory: section(() => import('./sections/memory'), 'MemorySection'),
  approvals: section(() => import('./sections/approvals'), 'ApprovalsSection'),
  environment: section(() => import('./sections/environment'), 'EnvironmentSection'),
  health: section(() => import('./sections/health'), 'HealthSection'),
  logs: section(() => import('./sections/logs'), 'LogsSection'),
  advanced: section(() => import('./sections/advanced'), 'AdvancedSection'),
  debug: section(() => import('./sections/debug'), 'DebugSection'),
  updates: section(() => import('./sections/system'), 'UpdatesSection'),
  about: section(() => import('./sections/system'), 'AboutSection'),
  setup: section(() => import('./sections/setup'), 'SetupSection'),
}

function SettingsSearch() {
  const [query, setQuery] = useState('')
  const fields = useSettingsFields((s) => s.bySection)
  const navigate = useNavigate()
  const results = searchSettings(query, fields)
  return (
    <div className="flex flex-col gap-2">
      <label className="relative block">
        <span className="sr-only">{t('webSettings.searchLabel')}</span>
        <Search
          className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
          aria-hidden
        />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('webSettings.searchPlaceholder')}
          className="h-9 w-full rounded-md border border-border-control bg-surface-0 pr-2 pl-8 touch:h-11 touch:text-base"
        />
      </label>
      {query.trim() ? (
        results.length ? (
          <ul
            role="list"
            aria-label={t('webSettings.searchLabel')}
            className="flex flex-col gap-0.5"
          >
            {results.map((r) => (
              <li key={`${r.section.id}:${r.field?.id ?? ''}`}>
                <button
                  type="button"
                  className="target w-full rounded-md px-2 py-1.5 text-left hover:bg-surface-2"
                  onClick={() => {
                    setQuery('')
                    navigate(
                      `/settings/${r.section.id}${r.field ? `?field=${encodeURIComponent(r.field.id)}` : ''}`,
                    )
                  }}
                >
                  {r.label}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p role="status" className="px-2 text-small text-text-muted">
            {t('webSettings.searchEmpty', { query: query.trim() })}
          </p>
        )
      ) : null}
    </div>
  )
}

function SettingsNav() {
  return (
    <nav aria-label={t('webSettings.hubTitle')} className="flex flex-col gap-4">
      <SettingsSearch />
      {SETTINGS_GROUPS.map((group) => (
        <section key={group.id} aria-label={t(group.titleKey)}>
          <h2 className="px-2 pb-1 text-small font-semibold tracking-wide text-text-muted uppercase">
            {t(group.titleKey)}
          </h2>
          <ul role="list" className="flex flex-col gap-0.5">
            {SETTINGS_SECTIONS.filter((s) => s.group === group.id).map((s) => (
              <li key={s.id}>
                <NavLink
                  to={`/settings/${s.id}`}
                  className={({ isActive }) =>
                    cn(
                      'target block rounded-md px-2 py-1.5',
                      isActive ? 'bg-surface-3 font-medium' : 'hover:bg-surface-2',
                    )
                  }
                >
                  {t(s.titleKey)}
                </NavLink>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </nav>
  )
}

function useSettingsSnapshot() {
  const rpc = useRpc()
  return useQuery({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
  })
}

function SettingsSummary() {
  const query = useSettingsSnapshot()
  if (query.isPending) {
    return (
      <LoadingRegion>
        <Skeleton className="h-24" />
      </LoadingRegion>
    )
  }
  if (query.isError)
    return (
      <ErrorState title={t('webSettings.summaryFailed')} onRetry={() => void query.refetch()} />
    )
  const r = readinessFromSnapshot(query.data)
  const llm = query.data.config?.llm ?? {}
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="flex-1 text-xl font-semibold">{t('webSettings.summaryTitle')}</h1>
        <Button
          size="sm"
          variant="outline"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          <RefreshCw aria-hidden className={cn(query.isFetching && 'animate-spin')} />
          {t('webSettings.reload')}
        </Button>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1" data-testid="settings-active">
        <dt className="text-text-muted">{t('webSettings.summaryProvider')}</dt>
        <dd className="font-mono">{llm.provider || t('webSettings.summaryNone')}</dd>
        <dt className="text-text-muted">{t('webSettings.summaryModel')}</dt>
        <dd className="font-mono break-all">{llm.model || t('webSettings.summaryNone')}</dd>
      </dl>
      <p data-testid="settings-readiness">
        {t('webSettings.summaryReadiness', { ready: r.ready, total: r.total })}
        {r.actionRequired
          ? ` · ${tPlural('webSettings.summaryActionRequired', r.actionRequired)}`
          : ''}
        {query.data.pendingRestart ? ` · ${t('webSettings.summaryRestartNeeded')}` : ''}
        {query.data.writeBlocked ? ` · ${t('webSettings.summaryChangesPaused')}` : ''}
      </p>
      <div>
        <Link to="/settings/setup" className={buttonVariants({ variant: 'primary' })}>
          {t('webSettings.summarySetup')}
        </Link>
      </div>
      <ul role="list" className="grid gap-2 sm:grid-cols-2">
        {SETTINGS_SECTIONS.map((s) => (
          <li key={s.id}>
            <Link
              to={`/settings/${s.id}`}
              className="block rounded-lg border border-border p-3 hover:bg-surface-2"
            >
              <span className="block font-medium">{t(s.titleKey)}</span>
              <span className="block text-small text-text-muted">{t(s.descriptionKey)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * The restart notice, read from the configuration snapshot so it names what
 * needs the restart, survives a reload and shows a restart another client
 * caused. `restartRequired` from a write only stands in on gateways whose
 * snapshot does not report it.
 */
function RestartNotice() {
  const snapshot = useSettingsSnapshot()
  const requested = useRestartNotice((s) => s.requested)
  const dismissed = useRestartNotice((s) => s.dismissed)
  const dismiss = useRestartNotice((s) => s.dismiss)
  const queryClient = useQueryClient()
  const rpc = useRpc()
  const [busy, setBusy] = useState(false)
  const reported = snapshot.data?.pendingRestart
  const pending = typeof reported === 'boolean' ? reported || requested : requested
  const reasons = reported ? (snapshot.data?.restartReasons ?? []) : []
  const signature = reasons.length ? reasons.join(',') : 'restart'
  if (!pending || dismissed === signature) return null
  return (
    <div
      role="status"
      className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2"
    >
      <RotateCw className="size-4 text-warn" aria-hidden data-status-icon />
      <span className="min-w-0 flex-1">
        <strong>{t('webSettings.restartTitle')}</strong>{' '}
        {reasons.length
          ? t('webSettings.restartBody', { reasons: reasons.join(', ') })
          : t('webSettings.restartBodyGeneric')}
      </span>
      <Button
        size="sm"
        variant="primary"
        disabled={busy}
        onClick={() => {
          setBusy(true)
          void rpc
            .call('gateway.restart', { reason: 'settings' })
            .then(async () => {
              dismiss(signature)
              await queryClient.invalidateQueries({ queryKey: SETTINGS_SNAPSHOT_QUERY_KEY })
            })
            .catch(() => undefined)
            .finally(() => setBusy(false))
        }}
      >
        {busy ? <Loader2 className="animate-spin" /> : null}
        {busy ? t('webSettings.restarting') : t('webSettings.restartNow')}
      </Button>
      <Button size="sm" variant="ghost" onClick={() => dismiss(signature)}>
        {t('webSettings.restartLater')}
      </Button>
    </div>
  )
}

/** Moves focus to `?field=` after a settings-search jump. */
function useFocusField() {
  const { search, pathname } = useLocation()
  useEffect(() => {
    const field = new URLSearchParams(search).get('field')
    if (!field) return
    let tries = 0
    const id = window.setInterval(() => {
      const row = document.querySelector<HTMLElement>(`[data-field-id="${CSS.escape(field)}"]`)
      const target = row?.querySelector<HTMLElement>('input, select, textarea, button') ?? row
      if (target || ++tries > 40) {
        window.clearInterval(id)
        target?.focus()
      }
    }, 50)
    return () => window.clearInterval(id)
  }, [search, pathname])
}

/** `/settings` and `/settings/:section`: the hub (settings spec "Settings hub layout"). */
export function SettingsPage() {
  const { section } = useParams()
  useFocusField()
  const meta = section ? findSection(section) : null
  const Section = meta ? SECTION_COMPONENTS[meta.id] : null
  return (
    // Phones get master-detail: the section list, or one section with a way
    // back. Side by side, the long list squeezed the section pane to 0 px.
    <div className="flex min-h-0 flex-col md:h-full md:flex-row">
      <aside
        className={cn(
          'shrink-0 border-border p-3 md:w-64 md:overflow-y-auto md:border-r max-md:border-b',
          section && 'max-md:hidden',
        )}
      >
        <SettingsNav />
      </aside>
      {/* Its own scroller from md up: focusable so a section with nothing to
          Tab to (e.g. Notifications without Web Push) still scrolls by keyboard. */}
      <div
        role="region"
        aria-label={
          meta
            ? t('webSettings.sectionPane', { section: t(meta.titleKey) })
            : t('webShell.pageSettings')
        }
        tabIndex={0}
        className="min-w-0 flex-1 focus-visible:-outline-offset-2 md:overflow-auto md:overscroll-contain"
      >
        <div className="mx-auto w-full max-w-3xl p-4 md:p-6">
          {section ? (
            <Link
              to="/settings"
              className="target mb-3 -ml-1 inline-flex items-center gap-1 rounded-md text-small text-text-muted hover:text-text md:hidden"
            >
              <ChevronLeft className="size-4" aria-hidden />
              {t('webSettings.allSettings')}
            </Link>
          ) : null}
          <RestartNotice />
          {!section ? (
            <SettingsSummary />
          ) : Section && meta ? (
            <Suspense
              fallback={
                <LoadingRegion>
                  <Skeleton className="h-40" />
                </LoadingRegion>
              }
            >
              <h1 className="mb-1 text-xl font-semibold">{t(meta.titleKey)}</h1>
              <p className="mb-4 text-text-muted">{t(meta.descriptionKey)}</p>
              <Section />
            </Suspense>
          ) : (
            <EmptyState title={t('webSettings.sectionNotFound', { section: section })} />
          )}
        </div>
      </div>
    </div>
  )
}
