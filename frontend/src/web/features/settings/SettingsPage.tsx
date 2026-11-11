import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  Bot,
  Brain,
  Bug,
  ChevronLeft,
  Cpu,
  Download,
  Globe,
  HeartPulse,
  Image as ImageIcon,
  Info,
  KeyRound,
  Loader2,
  MessagesSquare,
  Palette,
  Plug,
  Puzzle,
  RefreshCw,
  RotateCw,
  ScrollText,
  Search,
  SearchX,
  Server,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  type LucideIcon,
} from 'lucide-react'
import { lazy, Suspense, useEffect, useState, type ComponentType } from 'react'
import { Link, NavLink, useLocation, useNavigate, useParams } from 'react-router'
import { useRpc } from '@/app/providers'
import { t, tPlural, type MessageKey } from '@/i18n'
import '@/i18n/en/webSettings'
import '@/i18n/en/webShell'
import { cn } from '@/lib/utils'
import {
  loadSettingsSnapshot,
  readinessFromSnapshot,
  SETTINGS_SNAPSHOT_QUERY_KEY,
} from '@/views/settings/snapshot'
import { Button, buttonVariants } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { Input } from '@/web/components/ui/input'
import { SectionHeader } from '@/web/components/ui/section-header'
import { useGlide } from '@/web/components/ui/use-glide'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useRestartNotice } from './config-editor'
import { useSettingsFields } from './fields'
import { findSection, SETTINGS_GROUPS, SETTINGS_SECTIONS } from './registry'
import { searchSettings } from './search'
import { Notice } from './ui'

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

/** Lucide icon per section id (display only; the registry stays metadata). */
const SECTION_ICONS: Record<string, LucideIcon> = {
  appearance: Palette,
  notifications: Bell,
  gateway: Server,
  channels: MessagesSquare,
  agents: Bot,
  models: Cpu,
  search: Globe,
  skills: Puzzle,
  mcp: Plug,
  memory: Brain,
  media: ImageIcon,
  approvals: ShieldCheck,
  environment: KeyRound,
  health: HeartPulse,
  logs: ScrollText,
  advanced: SlidersHorizontal,
  debug: Bug,
  updates: Download,
  about: Info,
  setup: Sparkles,
}

export function SectionIcon({ id, className }: { id: string; className?: string }) {
  const Icon = SECTION_ICONS[id] ?? Settings2
  return <Icon className={className} aria-hidden />
}

/** `text` with every case-insensitive occurrence of `query` wrapped in <mark>. */
function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim().toLowerCase()
  const i = q ? text.toLowerCase().indexOf(q) : -1
  if (i < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, i)}
      <mark>{text.slice(i, i + q.length)}</mark>
      <Highlight text={text.slice(i + q.length)} query={query} />
    </>
  )
}

function SettingsSearch() {
  const [query, setQuery] = useState('')
  const fields = useSettingsFields((s) => s.bySection)
  const navigate = useNavigate()
  const results = searchSettings(query, fields)
  return (
    <div className="flex flex-col gap-2">
      <label className="block">
        <span className="sr-only">{t('webSettings.searchLabel')}</span>
        <Input
          type="search"
          icon={<Search />}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('webSettings.searchPlaceholder')}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && query) {
              e.preventDefault()
              setQuery('')
            }
          }}
        />
      </label>
      {query.trim() ? (
        results.length ? (
          <ul
            role="list"
            aria-label={t('webSettings.searchLabel')}
            className="set-search-results flex max-h-[min(24rem,60vh)] flex-col gap-0.5 overflow-y-auto"
          >
            {results.map((r) => (
              <li key={`${r.section.id}:${r.field?.id ?? ''}`}>
                <button
                  type="button"
                  className="press target flex w-full items-start gap-2.5 rounded-control px-2 py-1.5 text-left hover:bg-surface-3 focus-visible:bg-surface-3"
                  onClick={() => {
                    setQuery('')
                    navigate(
                      `/settings/${r.section.id}${r.field ? `?field=${encodeURIComponent(r.field.id)}` : ''}`,
                    )
                  }}
                >
                  <SectionIcon
                    id={r.section.id}
                    className="mt-0.5 size-4 shrink-0 text-text-muted"
                  />
                  <span className="min-w-0 break-words">
                    <Highlight text={r.label} query={query} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p
            role="status"
            className="anim-fade-in flex items-center gap-2 rounded-control border border-dashed border-border px-3 py-2.5 text-small text-text-muted"
          >
            <SearchX className="size-4 shrink-0" aria-hidden />
            {t('webSettings.searchEmpty', { query: query.trim() })}
          </p>
        )
      ) : null}
    </div>
  )
}

function SettingsNav() {
  const track = useGlide<HTMLDivElement>()
  return (
    <nav aria-label={t('webSettings.hubTitle')} className="flex flex-col gap-4">
      <SettingsSearch />
      <div ref={track} className="set-nav glide-track flex flex-col gap-4">
        {SETTINGS_GROUPS.map((group) => (
          <section key={group.id} aria-label={t(group.titleKey)}>
            <SectionHeader as="h2" size="sm" title={t(group.titleKey)} className="px-2.5 pb-1.5" />
            <ul role="list" className="flex flex-col gap-px">
              {SETTINGS_SECTIONS.filter((s) => s.group === group.id).map((s) => (
                <li key={s.id}>
                  <NavLink to={`/settings/${s.id}`} className="set-nav-link target">
                    <SectionIcon id={s.id} />
                    <span className="min-w-0 truncate">{t(s.titleKey)}</span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
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
      <LoadingRegion className="flex flex-col gap-4">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-36 rounded-card" />
        <div className="grid gap-3 sm:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-20 rounded-card" />
          ))}
        </div>
      </LoadingRegion>
    )
  }
  if (query.isError)
    return (
      <ErrorState title={t('webSettings.summaryFailed')} onRetry={() => void query.refetch()} />
    )
  const r = readinessFromSnapshot(query.data)
  const llm = query.data.config?.llm ?? {}
  const percent = r.total ? Math.round((r.ready / r.total) * 100) : 0
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="set-title flex-1">{t('webSettings.summaryTitle')}</h1>
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
      <section className="set-card relative flex flex-col gap-4 overflow-hidden sm:flex-row sm:items-center">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 bg-[radial-gradient(60%_120%_at_100%_0%,var(--accent-tint),transparent_70%)]"
        />
        <div className="relative flex min-w-0 flex-1 flex-col gap-3">
          <dl
            className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-small"
            data-testid="settings-active"
          >
            <dt className="text-label text-text-muted">{t('webSettings.summaryProvider')}</dt>
            <dd className="font-mono">{llm.provider || t('webSettings.summaryNone')}</dd>
            <dt className="text-label text-text-muted">{t('webSettings.summaryModel')}</dt>
            <dd className="font-mono break-all">{llm.model || t('webSettings.summaryNone')}</dd>
          </dl>
          <div className="flex flex-col gap-1.5">
            <p data-testid="settings-readiness" className="text-small">
              {t('webSettings.summaryReadiness', { ready: r.ready, total: r.total })}
              {r.actionRequired
                ? ` · ${tPlural('webSettings.summaryActionRequired', r.actionRequired)}`
                : ''}
              {query.data.pendingRestart ? ` · ${t('webSettings.summaryRestartNeeded')}` : ''}
              {query.data.writeBlocked ? ` · ${t('webSettings.summaryChangesPaused')}` : ''}
            </p>
            <div
              aria-hidden
              className="h-1.5 max-w-sm overflow-hidden rounded-full bg-surface-inset"
            >
              <div
                className="h-full origin-left rounded-full bg-accent [background-image:var(--accent-gradient)] transition-[scale] duration-[var(--duration-slow)] ease-[var(--ease-out)]"
                style={{ scale: `${percent / 100} 1` }}
              />
            </div>
          </div>
        </div>
        <div className="relative">
          {/* Outline: the shell's setup bar carries the lime "Open guided
              setup" while setup is unfinished, and one view gets one primary. */}
          <Link to="/settings/setup" className={buttonVariants({ variant: 'outline' })}>
            <Sparkles aria-hidden />
            {t('webSettings.summarySetup')}
          </Link>
        </div>
      </section>
      {SETTINGS_GROUPS.map((group) => (
        <section key={group.id} aria-label={t(group.titleKey)} className="flex flex-col gap-2">
          <SectionHeader as="h2" size="sm" title={t(group.titleKey)} />
          <ul role="list" className="anim-stagger grid gap-2.5 sm:grid-cols-2">
            {SETTINGS_SECTIONS.filter((s) => s.group === group.id).map((s) => (
              <li key={s.id}>
                <Card asChild variant="interactive" padding="sm" className="flex h-full gap-3">
                  <Link to={`/settings/${s.id}`}>
                    <span
                      aria-hidden
                      className="grid size-8 shrink-0 place-items-center rounded-control bg-surface-2 text-text-muted shadow-highlight"
                    >
                      <SectionIcon id={s.id} className="size-4" />
                    </span>
                    <span className="min-w-0">
                      <span className="block font-medium">{t(s.titleKey)}</span>
                      <span className="block text-small text-text-muted">
                        {t(s.descriptionKey)}
                      </span>
                    </span>
                  </Link>
                </Card>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

const RESTART_REASON_KEYS: Record<string, MessageKey> = {
  memory: 'webSettings.restartReasonMemory',
  channels: 'webSettings.restartReasonChannels',
  sandbox: 'webSettings.restartReasonSandbox',
  gateway_bind: 'webSettings.restartReasonGatewayBind',
  task_runtime: 'webSettings.restartReasonTaskRuntime',
  tls: 'webSettings.restartReasonTls',
  cors: 'webSettings.restartReasonCors',
  control_ui: 'webSettings.restartReasonControlUi',
  server_debug: 'webSettings.restartReasonServerDebug',
  state_dir: 'webSettings.restartReasonStateDir',
  logging: 'webSettings.restartReasonLogging',
  mcp: 'webSettings.restartReasonMcp',
  tools_runtime: 'webSettings.restartReasonToolsRuntime',
  skill_loader: 'webSettings.restartReasonSkillLoader',
  heartbeat_watcher: 'webSettings.restartReasonHeartbeatWatcher',
  diagnostics: 'webSettings.restartReasonDiagnostics',
  config: 'webSettings.restartReasonConfig',
  provider: 'webSettings.restartReasonProvider',
  router: 'webSettings.restartReasonRouter',
  search: 'webSettings.restartReasonSearch',
  x_search: 'webSettings.restartReasonXSearch',
  image_generation: 'webSettings.restartReasonImageGeneration',
  audio: 'webSettings.restartReasonAudio',
}

/** A restart reason id as a reader sees it; an id this console does not know shows as is. */
export function restartReasonLabel(id: string): string {
  const key = RESTART_REASON_KEYS[id]
  return key ? t(key) : id
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
  // Docked at the bottom of the pane, in the save bar's place, rather than
  // at the top: it usually arrives right after a save, and inserting it
  // above the section pushed everything the reader was looking at down.
  return (
    <div className="set-restart-dock sticky bottom-4 z-[var(--z-sticky)] mt-8">
      <Notice
        role="status"
        tone="warn"
        icon={<RotateCw data-status-icon />}
        actions={
          <>
            <Button size="sm" variant="ghost" onClick={() => dismiss(signature)}>
              {t('webSettings.restartLater')}
            </Button>
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
              {busy ? <Loader2 className="animate-spin" /> : <RotateCw />}
              {busy ? t('webSettings.restarting') : t('webSettings.restartNow')}
            </Button>
          </>
        }
      >
        <p className="font-medium">{t('webSettings.restartTitle')}</p>
        <p className="text-small text-text-muted">
          {reasons.length
            ? t('webSettings.restartBody', { reasons: reasons.map(restartReasonLabel).join(', ') })
            : t('webSettings.restartBodyGeneric')}
        </p>
      </Notice>
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
          'set-aside shrink-0 border-border px-3 py-4 md:w-60 md:overflow-y-auto md:overscroll-contain md:border-r lg:w-64 max-md:border-b',
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
        className="set-pane bg-atmosphere min-w-0 flex-1 focus-visible:-outline-offset-2 md:overflow-auto md:overscroll-contain"
      >
        <div
          key={section ?? ''}
          className="anim-enter mx-auto w-full max-w-[52rem] px-4 pt-4 pb-10 md:px-8 md:pt-8"
        >
          {section ? (
            <Link
              to="/settings"
              className="target mb-3 -ml-1 inline-flex items-center gap-1 rounded-md text-small text-text-muted hover:text-text md:hidden"
            >
              <ChevronLeft className="size-4" aria-hidden />
              {t('webSettings.allSettings')}
            </Link>
          ) : null}
          {!section ? (
            <SettingsSummary />
          ) : Section && meta ? (
            <>
              <header className="mb-6 flex items-start gap-3.5">
                <span
                  aria-hidden
                  className="mt-0.5 grid size-10 shrink-0 place-items-center rounded-card border border-border bg-surface-1 text-accent shadow-card"
                >
                  <SectionIcon id={meta.id} className="size-5" />
                </span>
                <div className="min-w-0">
                  <h1 className="set-title">{t(meta.titleKey)}</h1>
                  <p className="mt-0.5 text-text-muted">{t(meta.descriptionKey)}</p>
                </div>
              </header>
              <Suspense
                fallback={
                  <LoadingRegion className="flex flex-col gap-4">
                    <Skeleton className="h-40 rounded-card" />
                    <Skeleton className="h-28 rounded-card" />
                  </LoadingRegion>
                }
              >
                <Section />
              </Suspense>
            </>
          ) : (
            <EmptyState
              icon={<SearchX />}
              title={t('webSettings.sectionNotFound', { section: section })}
              action={
                <Link to="/settings" className={buttonVariants({ variant: 'outline' })}>
                  {t('webSettings.allSettings')}
                </Link>
              }
            />
          )}
          <RestartNotice />
        </div>
      </div>
    </div>
  )
}
