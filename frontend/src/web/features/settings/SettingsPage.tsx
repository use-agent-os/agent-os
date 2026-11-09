import { useQuery } from '@tanstack/react-query'
import { Loader2, RotateCw, Search } from 'lucide-react'
import { lazy, Suspense, useEffect, useState, type ComponentType } from 'react'
import { Link, NavLink, useLocation, useNavigate, useParams } from 'react-router'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webSettings'
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

function SettingsSummary() {
  const rpc = useRpc()
  const query = useQuery({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
  })
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
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">{t('webSettings.summaryTitle')}</h1>
      <p data-testid="settings-readiness">
        {t('webSettings.summaryReadiness', { ready: r.ready, total: r.total })}
        {r.actionRequired
          ? ` · ${tPlural('webSettings.summaryActionRequired', r.actionRequired)}`
          : ''}
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

function RestartNotice() {
  const reasons = useRestartNotice((s) => s.reasons)
  const clear = useRestartNotice((s) => s.clear)
  const rpc = useRpc()
  const [busy, setBusy] = useState(false)
  if (reasons.length === 0) return null
  return (
    <div
      role="status"
      className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2"
    >
      <RotateCw className="size-4 text-warn" aria-hidden data-status-icon />
      <span className="min-w-0 flex-1">
        <strong>{t('webSettings.restartTitle')}</strong>{' '}
        {t('webSettings.restartBody', { reasons: reasons.join(', ') })}
      </span>
      <Button
        size="sm"
        variant="primary"
        disabled={busy}
        onClick={() => {
          setBusy(true)
          void rpc
            .call('gateway.restart', { reason: 'settings' })
            .then(() => clear())
            .catch(() => undefined)
            .finally(() => setBusy(false))
        }}
      >
        {busy ? <Loader2 className="animate-spin" /> : null}
        {busy ? t('webSettings.restarting') : t('webSettings.restartNow')}
      </Button>
      <Button size="sm" variant="ghost" onClick={clear}>
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
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      <aside className="shrink-0 overflow-y-auto border-border p-3 md:w-64 md:border-r max-md:border-b">
        <SettingsNav />
      </aside>
      <div className="scroll-contained min-w-0 flex-1">
        <div className="mx-auto w-full max-w-3xl p-4 md:p-6">
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
