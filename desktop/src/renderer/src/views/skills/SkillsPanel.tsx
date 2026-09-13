import './skills.css'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Download,
  Globe,
  Package,
  RefreshCw,
  Search,
  Sparkles,
  TriangleAlert,
  X,
} from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import { t as tw, tPlural } from '@/i18n'
import {
  categoryChips,
  communityFilter,
  filterRegistry,
  filterSkills,
  firstUpdateResult,
  groupSkills,
  installAction,
  installSource,
  installedEmptyMessage,
  markInstalled,
  mergeRegistryRows,
  partnerEmptyMessage,
  registryEmptyMessage,
  registryKey,
  REGISTRY_SEARCH_DEBOUNCE_MS,
  skillStats,
  skillsByPublisher,
  stillMissingCount,
  type DepsInstallResult,
  type RawSkill,
  type RegistryItem,
  type StatusFilter,
  type UpdateResult,
} from '@/views/skills/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useGateway } from '~/stores/gateway'
import { useUi } from '~/stores/ui'
import {
  forgetSessionInstall,
  HIDDEN_COMMUNITY_SOURCES,
  isRegistrySource,
  PARTNER_LABEL,
  panelSubtitle,
  resolveSelection,
  sessionInstallsFor,
  SKILL_SOURCES,
  skillPrefillText,
  skillRowKey,
  type RegistrySource,
  type SkillSource,
} from './logic'
import { RegistryDetail } from './RegistryDetail'
import { RemoveConfirm, SetEnvSheet } from './sheets'
import { InstalledList, RegistryList } from './SkillList'
import { SkillDetail } from './SkillDetail'
import { BrandMark, RegistryMark } from './SkillMark'

interface SkillsListResponse {
  skills?: RawSkill[]
}
interface SearchResponse {
  results?: RegistryItem[]
}
interface InstallResponse {
  success?: boolean
  name?: string
  message?: string
  scan_verdict?: string
  scan_findings?: unknown[]
}
interface MutationResponse {
  success?: boolean
  message?: string
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

const STATUS_FILTERS: readonly StatusFilter[] = ['all', 'ready', 'needs-setup', 'disabled']

/**
 * Skills as a panel over the window, like Scheduled jobs: sources on the
 * left the way Mail lists mailboxes, the rows of the chosen source in the
 * middle, the selected skill on the right. Escape or the close button to
 * leave. It reads the `skillsOpen` flag from the UI store; the sidebar and
 * ⌘⇧K toggle it.
 */
export function SkillsPanel() {
  const open = useUi((s) => s.skillsOpen)
  const close = useUi((s) => s.closeSkills)
  const titleId = useId()
  if (!open) return null
  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={close}
      overlayClassName="skills-panel__overlay"
      className="skills-panel"
    >
      <SkillsBody titleId={titleId} onClose={close} />
    </ModalShell>
  )
}

function SkillsBody({ titleId, onClose }: { titleId: string; onClose: () => void }) {
  const gatewayState = useGateway((s) => s.status.state)
  const connected = gatewayState === 'running'
  return connected ? (
    <ConnectedSkills titleId={titleId} onClose={onClose} />
  ) : (
    <>
      <PanelHeader titleId={titleId} subtitle="" onClose={onClose} />
      <div className="skills-offline">
        <Package className="size-9 text-dim" strokeWidth={1.25} aria-hidden />
        <p className="text-[15px] font-semibold text-foreground">
          {gatewayState === 'starting'
            ? t('chat.waitingGateway')
            : t(`gateway.state.${gatewayState}`)}
        </p>
        <p className="max-w-sm">{t('chat.gatewayDown')}</p>
      </div>
    </>
  )
}

function PanelHeader({
  titleId,
  subtitle,
  onClose,
}: {
  titleId: string
  subtitle: string
  onClose: () => void
}) {
  return (
    <header className="skills-panel__head">
      <div>
        <h1 id={titleId}>{t('skills.title')}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      <Button
        variant="ghost"
        size="icon"
        aria-label={t('skills.close')}
        title={t('skills.close')}
        onClick={onClose}
      >
        <X className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
    </header>
  )
}

/** One `skills.search` snapshot per catalog, fetched when its source is shown. */
function useCatalog(source: RegistrySource, enabled: boolean) {
  const rpc = useRpc()
  return useQuery<RegistryItem[]>({
    queryKey: ['skills.search', source],
    enabled,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      const params =
        source === 'community' ? { query: '', limit: 500 } : { query: '', limit: 500, source }
      const data = await rpc.call<SearchResponse>('skills.search', params)
      const rows = data.results ?? []
      return source === 'community' ? communityFilter(rows, HIDDEN_COMMUNITY_SOURCES) : rows
    },
  })
}

function ConnectedSkills({ titleId, onClose }: { titleId: string; onClose: () => void }) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const setPendingPrompt = useUi((s) => s.setPendingPrompt)

  const [source, setSource] = useState<SkillSource>('installed')
  const [selected, setSelected] = useState<Partial<Record<SkillSource, string | null>>>({})
  const [installedText, setInstalledText] = useState('')
  const [installedStatus, setInstalledStatus] = useState<StatusFilter>('all')
  const [robinhoodText, setRobinhoodText] = useState('')
  const [robinhoodStatus, setRobinhoodStatus] = useState<StatusFilter>('all')
  const [catalogText, setCatalogText] = useState<Record<RegistrySource, string>>({
    bankr: '',
    aeon: '',
    capminal: '',
    community: '',
  })
  const [catalogCat, setCatalogCat] = useState<Record<RegistrySource, string>>({
    bankr: 'all',
    aeon: 'all',
    capminal: 'all',
    community: 'all',
  })
  // The committed community query: a non-empty one hits the server, since the
  // snapshot only covers each hub's first page.
  const [communityQuery, setCommunityQuery] = useState('')
  const [githubUrl, setGithubUrl] = useState('')
  const [forceArmed, setForceArmed] = useState<Set<string>>(new Set())
  const [busyKeys, setBusyKeys] = useState<Set<string>>(new Set())
  // Catalog rows installed this session, kept on screen until the refetch
  // brings the server's own row for them.
  const [sessionInstalls, setSessionInstalls] = useState<RegistryItem[]>([])
  const [envPrompt, setEnvPrompt] = useState<{ skill: string; name: string } | null>(null)
  const [envSaving, setEnvSaving] = useState(false)
  const [pendingRemove, setPendingRemove] = useState<RawSkill | null>(null)

  useEffect(() => {
    const id = setTimeout(
      () => setCommunityQuery(catalogText.community.trim()),
      REGISTRY_SEARCH_DEBOUNCE_MS,
    )
    return () => clearTimeout(id)
  }, [catalogText.community])

  // ── Installed skills ──────────────────────────────────────────────────
  const skillsQuery = useQuery<RawSkill[]>({
    queryKey: ['skills'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<SkillsListResponse>('skills.list', {})
      return data.skills ?? []
    },
    refetchOnWindowFocus: false,
  })

  useEffect(() => {
    if (skillsQuery.isError) {
      toast.error(tw('skills.toastLoadFailed', { message: errorText(skillsQuery.error) }), {
        id: 'skills-load-err',
      })
    }
  }, [skillsQuery.isError, skillsQuery.error])

  // ── Catalogs ──────────────────────────────────────────────────────────
  const bankr = useCatalog('bankr', source === 'bankr')
  const aeon = useCatalog('aeon', source === 'aeon')
  const capminal = useCatalog('capminal', source === 'capminal')
  const community = useCatalog('community', source === 'community')
  const communitySearch = useQuery<RegistryItem[]>({
    queryKey: ['skills.search', 'community', communityQuery],
    enabled: source === 'community' && communityQuery.length > 0,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<SearchResponse>('skills.search', {
        query: communityQuery,
        limit: 100,
      })
      return communityFilter(data.results ?? [], HIDDEN_COMMUNITY_SOURCES)
    },
  })

  const invalidateSkills = () => queryClient.invalidateQueries({ queryKey: ['skills'] })
  const invalidateRegistry = () => queryClient.invalidateQueries({ queryKey: ['skills.search'] })
  // Flip the Installed mark on every cached catalog list before the refetch
  // lands; lists still in flight are left alone.
  const markCached = (identifier: string, name: string, installed: boolean) =>
    queryClient.setQueriesData<RegistryItem[]>({ queryKey: ['skills.search'] }, (old) =>
      old ? markInstalled(old, identifier, name, installed) : old,
    )
  const setBusy = (key: string, on: boolean) =>
    setBusyKeys((prev) => {
      const next = new Set(prev)
      if (on) next.add(key)
      else next.delete(key)
      return next
    })
  const armForce = (key: string, on: boolean) =>
    setForceArmed((prev) => {
      const next = new Set(prev)
      if (on) next.add(key)
      else next.delete(key)
      return next
    })

  // ── Mutations ─────────────────────────────────────────────────────────
  const installMutation = useMutation({
    mutationFn: (vars: {
      identifier: string
      source: string
      force: boolean
      item?: RegistryItem
    }) =>
      rpc.call<InstallResponse>('skills.install', {
        identifier: vars.identifier,
        source: vars.source,
        force: vars.force,
      }),
    onMutate: (vars) => setBusy(vars.identifier, true),
    onSettled: (_d, _e, vars) => setBusy(vars.identifier, false),
    onSuccess: (res, vars) => {
      if (res?.success) {
        armForce(vars.identifier, false)
        toast.success(tw('skills.toastInstalled', { name: res.name || vars.identifier }), {
          id: 'skills-install',
        })
        if (vars.item) {
          const row = { ...vars.item, installed: true }
          setSessionInstalls((prev) => mergeRegistryRows([row], prev))
        }
        if (vars.source === 'github') setGithubUrl('')
        markCached(vars.identifier, res.name || '', true)
        void invalidateSkills()
        void invalidateRegistry()
        return
      }
      // A dangerous scan verdict is not an error: it arms an explicit override.
      const blocked = res?.scan_verdict === 'dangerous'
      const n = (res?.scan_findings || []).length
      if (blocked && !vars.force) {
        armForce(vars.identifier, true)
        const name = res?.name || tw('skills.toastScanUnnamed')
        toast.error(
          tw('skills.toastScanBlocked', {
            target: n ? tPlural('skills.toastScanTarget', n, { name }) : name,
          }),
          { id: 'skills-install-err' },
        )
      } else {
        toast.error(res?.message || tw('skills.toastInstallFailed'), { id: 'skills-install-err' })
      }
    },
    onError: (err) => toast.error(errorText(err), { id: 'skills-install-err' }),
  })

  const uninstallMutation = useMutation({
    mutationFn: (vars: { name: string; identifier: string }) =>
      rpc.call<MutationResponse>('skills.uninstall', { name: vars.name }),
    onMutate: (vars) => setBusy('uninstall:' + vars.name, true),
    onSettled: (_d, _e, vars) => setBusy('uninstall:' + vars.name, false),
    onSuccess: (res, vars) => {
      if (res?.success) {
        toast.success(tw('skills.toastRemoved', { name: vars.name }), { id: 'skills-uninstall' })
        setPendingRemove(null)
        setSessionInstalls((prev) => forgetSessionInstall(prev, vars.name, vars.identifier))
        markCached(vars.identifier, vars.name, false)
        void invalidateSkills()
        // The catalog's Installed mark comes off the lockfile this just edited.
        void invalidateRegistry()
      } else {
        toast.error(res?.message || tw('skills.toastUninstallFailed'), {
          id: 'skills-uninstall-err',
        })
      }
    },
    onError: (err) => toast.error(errorText(err), { id: 'skills-uninstall-err' }),
  })

  const updateMutation = useMutation({
    mutationFn: (name: string) => rpc.call<UpdateResult>('skills.update', { name }),
    onMutate: (name) => setBusy('update:' + name, true),
    onSettled: (_d, _e, name) => setBusy('update:' + name, false),
    onSuccess: (res, name) => {
      const result = firstUpdateResult(res)
      if (result.success) {
        toast.success(result.message || tw('skills.toastUpdated', { name }), {
          id: 'skills-update',
        })
        void invalidateSkills()
      } else {
        toast.error(result.message || res?.message || tw('skills.toastUpdateFailed'), {
          id: 'skills-update-err',
        })
      }
    },
    onError: (err) => toast.error(errorText(err), { id: 'skills-update-err' }),
  })

  const depsMutation = useMutation({
    mutationFn: (vars: { name: string; installId: string }) =>
      rpc.call<DepsInstallResult>('skills.deps.install', {
        name: vars.name,
        install_id: vars.installId,
      }),
    onMutate: (vars) => setBusy('deps:' + vars.name + ':' + vars.installId, true),
    onSettled: (_d, _e, vars) => setBusy('deps:' + vars.name + ':' + vars.installId, false),
    onSuccess: (res) => {
      if (res?.success) {
        toast.success(res.message || tw('skills.toastDepsInstalled'), { id: 'skills-deps' })
        if (stillMissingCount(res) > 0) {
          toast.warning(res.message || tw('skills.toastInstallFailed'), { id: 'skills-deps-more' })
        }
      } else {
        toast.error(res?.message || tw('skills.toastInstallFailed'), { id: 'skills-deps-err' })
      }
      void invalidateSkills()
    },
    onError: (err) => toast.error(errorText(err), { id: 'skills-deps-err' }),
  })

  async function saveEnv(value: string) {
    if (!envPrompt) return
    setEnvSaving(true)
    try {
      await rpc.call('env.set', { name: envPrompt.name, value })
      await invalidateSkills()
      toast.success(tw('skills.toastEnvSaved', { name: envPrompt.name }), { id: 'skills-env' })
      setEnvPrompt(null)
    } catch (err) {
      toast.error(errorText(err), { id: 'skills-env-err' })
    } finally {
      setEnvSaving(false)
    }
  }

  // "Use" hands the skill to the composer as a prefill, nothing is sent. The
  // panel closes first so the chat is what the user lands on.
  function openInChat(name: string) {
    setPendingPrompt(skillPrefillText(name))
    onClose()
    if (!pathname.startsWith('/sessions')) void navigate('/sessions')
  }

  const runInstall = (item: RegistryItem, force: boolean) =>
    installMutation.mutate({
      identifier: registryKey(item),
      source: installSource(item),
      force,
      item,
    })

  const installFromGithub = () => {
    const url = githubUrl.trim()
    if (!url) return
    installMutation.mutate({ identifier: url, source: 'github', force: false })
  }

  // ── Derivations ───────────────────────────────────────────────────────
  const allSkills = skillsQuery.data ?? []
  const stats = skillStats(allSkills)
  const installedRows = filterSkills(allSkills, installedText, installedStatus)
  const groups = groupSkills(installedRows)
  const robinhoodAll = skillsByPublisher(allSkills, 'robinhood')
  const robinhoodRows = filterSkills(robinhoodAll, robinhoodText.trim(), robinhoodStatus)
  const robinhoodStats = skillStats(robinhoodAll)

  const catalogQuery = { bankr, aeon, capminal, community } as const
  // A row installed while browsing survives the refetch gap: the session's
  // own rows are merged under the server's, which wins on a collision.
  const catalogRows = useMemo<Record<RegistrySource, RegistryItem[]>>(
    () => ({
      bankr: mergeRegistryRows(bankr.data ?? [], sessionInstallsFor('bankr', sessionInstalls)),
      aeon: mergeRegistryRows(aeon.data ?? [], sessionInstallsFor('aeon', sessionInstalls)),
      capminal: mergeRegistryRows(
        capminal.data ?? [],
        sessionInstallsFor('capminal', sessionInstalls),
      ),
      community: mergeRegistryRows(
        community.data ?? [],
        sessionInstallsFor('community', sessionInstalls),
      ),
    }),
    [bankr.data, aeon.data, capminal.data, community.data, sessionInstalls],
  )

  const communityLive = communityQuery ? communitySearch.data : undefined
  const communityBase = communityLive ?? catalogRows.community
  const communityServerFiltered =
    Boolean(communityLive) && catalogText.community.trim() === communityQuery
  const communityError =
    communityQuery && communitySearch.isError
      ? errorText(communitySearch.error)
      : community.isError
        ? errorText(community.error)
        : ''

  const registrySource = isRegistrySource(source) ? source : null
  const registryBase = registrySource
    ? registrySource === 'community'
      ? communityBase
      : catalogRows[registrySource]
    : []
  const registryChips = registrySource
    ? categoryChips(
        registrySource === 'community' ? catalogRows.community : registryBase,
        catalogCat[registrySource],
      )
    : []
  const registryRows = registrySource
    ? filterRegistry(registryBase, catalogCat[registrySource], catalogText[registrySource], {
        serverFiltered: registrySource === 'community' && communityServerFiltered,
      })
    : []
  const registryLoading = registrySource
    ? registrySource === 'community'
      ? communityQuery
        ? communitySearch.isLoading
        : community.isLoading
      : catalogQuery[registrySource].isLoading
    : false
  const registryError = registrySource
    ? registrySource === 'community'
      ? communityError
      : catalogQuery[registrySource].isError
        ? errorText(catalogQuery[registrySource].error)
        : ''
    : ''

  // Selection per source, always resolving to a visible row when there is one.
  const selectedSkill =
    source === 'installed'
      ? resolveSelection(installedRows, skillRowKey, selected.installed ?? null)
      : source === 'robinhood'
        ? resolveSelection(robinhoodRows, skillRowKey, selected.robinhood ?? null)
        : null
  const selectedItem = registrySource
    ? resolveSelection(registryRows, registryKey, selected[registrySource] ?? null)
    : null
  const select = (key: string) => setSelected((prev) => ({ ...prev, [source]: key }))

  const refresh = () => {
    if (registrySource) {
      void catalogQuery[registrySource].refetch()
      if (registrySource === 'community' && communityQuery) void communitySearch.refetch()
    } else {
      void invalidateSkills()
    }
  }
  const fetching = registrySource
    ? catalogQuery[registrySource].isFetching || communitySearch.isFetching
    : skillsQuery.isFetching

  const subtitle = skillsQuery.isSuccess ? panelSubtitle(stats.total, stats.ready, stats.needs) : ''

  // ── Column 2 controls per source ──────────────────────────────────────
  const searchValue =
    source === 'installed'
      ? installedText
      : source === 'robinhood'
        ? robinhoodText
        : catalogText[source]
  const searchPlaceholder =
    source === 'installed'
      ? t('skills.search')
      : tw('skills.registrySearchPlaceholder', {
          label:
            source === 'community' ? tw('skills.registryLabelCommunity') : PARTNER_LABEL[source],
        })
  const onSearch = (value: string) => {
    if (source === 'installed') setInstalledText(value)
    else if (source === 'robinhood') setRobinhoodText(value)
    else setCatalogText((prev) => ({ ...prev, [source]: value }))
  }

  const statusFilterValue = source === 'installed' ? installedStatus : robinhoodStatus
  const statusStats = source === 'installed' ? stats : robinhoodStats
  const statusFilters = STATUS_FILTERS.filter(
    (f) =>
      f === 'all' ||
      f === 'ready' ||
      f === 'needs-setup' ||
      statusStats.disabled > 0 ||
      statusFilterValue === 'disabled',
  )
  const statusCount = (f: StatusFilter) =>
    f === 'all'
      ? statusStats.total
      : f === 'ready'
        ? statusStats.ready
        : f === 'needs-setup'
          ? statusStats.needs
          : statusStats.disabled
  const statusLabel = (f: StatusFilter) =>
    f === 'all'
      ? t('skills.filter.all')
      : f === 'ready'
        ? t('skills.filter.ready')
        : f === 'needs-setup'
          ? t('skills.filter.needsSetup')
          : t('skills.filter.disabled')

  function onRailKey(e: React.KeyboardEvent) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    const i = SKILL_SOURCES.indexOf(source)
    e.preventDefault()
    const next =
      SKILL_SOURCES[
        (i + (e.key === 'ArrowDown' ? 1 : SKILL_SOURCES.length - 1)) % SKILL_SOURCES.length
      ]
    if (next) {
      setSource(next)
      document.getElementById(`sk-source-${next}`)?.focus()
    }
  }

  const sourceLabel = (s: SkillSource) =>
    s === 'installed'
      ? t('skills.source.installed')
      : s === 'community'
        ? t('skills.source.community')
        : PARTNER_LABEL[s]
  const sourceHint = (s: SkillSource) =>
    s === 'installed'
      ? t('skills.source.installed.hint')
      : s === 'community'
        ? t('skills.source.community.hint')
        : s === 'robinhood'
          ? t('skills.source.robinhood.hint')
          : t('skills.source.partner.hint')

  return (
    <>
      <PanelHeader titleId={titleId} subtitle={subtitle} onClose={onClose} />
      <div className="sk">
        <nav className="sk-sources" aria-label={t('skills.sources')} onKeyDown={onRailKey}>
          <div className="sk-sources__label">{t('skills.sources')}</div>
          {SKILL_SOURCES.map((s) => (
            <button
              key={s}
              id={`sk-source-${s}`}
              type="button"
              className="sk-source app-no-drag"
              aria-current={s === source ? 'page' : undefined}
              onClick={() => setSource(s)}
            >
              <span className="sk-source__icon" aria-hidden>
                {s === 'installed' ? (
                  <Package className="size-4" strokeWidth={1.75} />
                ) : s === 'community' ? (
                  <Globe className="size-4" strokeWidth={1.75} />
                ) : (
                  <BrandMark brand={s} size="sm" />
                )}
              </span>
              <span className="sk-source__body">
                <span className="sk-source__name">{sourceLabel(s)}</span>
                <span className="sk-source__hint">{sourceHint(s)}</span>
              </span>
              {s === 'installed' && skillsQuery.isSuccess ? (
                <span className="sk-source__count">{stats.total}</span>
              ) : s === 'robinhood' && skillsQuery.isSuccess ? (
                <span className="sk-source__count">{robinhoodAll.length}</span>
              ) : null}
            </button>
          ))}
        </nav>

        <section className="sk-list" aria-label={sourceLabel(source)}>
          <div className="sk-list__head">
            <label className="mac-search app-no-drag flex-1">
              <Search className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
              <input
                type="search"
                placeholder={searchPlaceholder}
                aria-label={searchPlaceholder}
                autoComplete="off"
                value={searchValue}
                onChange={(e) => onSearch(e.target.value)}
              />
            </label>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('skills.refresh')}
              title={t('skills.refresh')}
              disabled={fetching}
              onClick={refresh}
            >
              <RefreshCw
                className={`size-4 text-muted-foreground${fetching ? ' sk-spin' : ''}`}
                strokeWidth={1.75}
                aria-hidden
              />
            </Button>
          </div>

          {!registrySource ? (
            <div role="radiogroup" aria-label={t('skills.filter')} className="sk-filters">
              {statusFilters.map((f) => (
                <button
                  key={f}
                  type="button"
                  role="radio"
                  aria-checked={statusFilterValue === f}
                  className="sk-filter"
                  data-filter={f}
                  onClick={() =>
                    source === 'installed' ? setInstalledStatus(f) : setRobinhoodStatus(f)
                  }
                >
                  <span>{statusLabel(f)}</span>
                  <span className="sk-filter__count">{statusCount(f)}</span>
                </button>
              ))}
            </div>
          ) : registryChips.length ? (
            <div role="radiogroup" aria-label={t('skills.category')} className="sk-filters">
              {registryChips.map((c) => (
                <button
                  key={c.cat}
                  type="button"
                  role="radio"
                  aria-checked={c.active}
                  className="sk-filter"
                  onClick={() => setCatalogCat((prev) => ({ ...prev, [registrySource]: c.cat }))}
                >
                  <span>{c.label}</span>
                  <span className="sk-filter__count">{c.count}</span>
                </button>
              ))}
            </div>
          ) : null}

          <div className="sk-list__scroll">
            {source === 'installed' ? (
              <InstalledList
                groups={groups}
                loading={skillsQuery.isPending}
                error={skillsQuery.isError ? t('skills.list.error') : null}
                emptyText={installedEmptyMessage(installedText, installedStatus)}
                selectedKey={selectedSkill ? skillRowKey(selectedSkill) : null}
                onSelect={select}
              />
            ) : source === 'robinhood' ? (
              <InstalledList
                groups={
                  robinhoodRows.length
                    ? [
                        {
                          key: 'partners',
                          label: PARTNER_LABEL.robinhood,
                          help: tw('skills.groupHelpPartners'),
                          skills: robinhoodRows,
                        },
                      ]
                    : []
                }
                loading={skillsQuery.isPending}
                error={skillsQuery.isError ? t('skills.list.error') : null}
                emptyText={partnerEmptyMessage(
                  PARTNER_LABEL.robinhood,
                  robinhoodText,
                  robinhoodStatus,
                )}
                selectedKey={selectedSkill ? skillRowKey(selectedSkill) : null}
                onSelect={select}
              />
            ) : (
              <RegistryList
                label={sourceLabel(source)}
                items={registryRows}
                loading={registryLoading}
                error={registryError || null}
                emptyText={registryEmptyMessage(source, catalogText[source])}
                selectedKey={selectedItem ? registryKey(selectedItem) : null}
                onSelect={select}
              />
            )}
          </div>
        </section>

        <section className="sk-detail" aria-live="polite">
          {source === 'robinhood' ? (
            <SourceIntro
              brand="robinhood"
              title={tw('skills.robinhoodTitle')}
              description={tw('skills.robinhoodDesc')}
              notice={tw('skills.robinhoodNotice')}
              count={robinhoodAll.length}
            />
          ) : source === 'bankr' ? (
            <SourceIntro
              brand="bankr"
              title={tw('skills.bankrTitle')}
              description={tw('skills.bankrDesc')}
              notice={tw('skills.bankrNotice')}
              count={catalogRows.bankr.length}
            />
          ) : source === 'aeon' ? (
            <SourceIntro
              brand="aeon"
              title={tw('skills.aeonTitle')}
              description={tw('skills.aeonDesc')}
              notice={tw('skills.aeonNotice')}
              count={catalogRows.aeon.length}
            />
          ) : source === 'capminal' ? (
            <SourceIntro
              brand="capminal"
              title={tw('skills.capminalTitle')}
              description={tw('skills.capminalDesc')}
              notice={tw('skills.capminalNotice')}
              count={catalogRows.capminal.length}
            />
          ) : source === 'community' ? (
            <div className="sk-intro">
              <span className="sk-intro__glyph" aria-hidden>
                <Globe className="size-5" strokeWidth={1.5} />
              </span>
              <div className="sk-intro__copy">
                <h2>{tw('skills.communityTitle')}</h2>
                <p>{tw('skills.communityDesc')}</p>
              </div>
              <form
                className="sk-github"
                aria-label={t('skills.github.title')}
                onSubmit={(e) => {
                  e.preventDefault()
                  installFromGithub()
                }}
              >
                <span className="sk-github__icon" aria-hidden>
                  <Download className="size-3.5" strokeWidth={1.75} />
                </span>
                <input
                  type="url"
                  className="mac-input sk-github__input"
                  placeholder={t('skills.github.placeholder')}
                  aria-label={t('skills.github.title')}
                  autoComplete="off"
                  value={githubUrl}
                  onChange={(e) => setGithubUrl(e.target.value)}
                />
                <Button
                  type="submit"
                  variant="primary"
                  disabled={!githubUrl.trim() || busyKeys.has(githubUrl.trim())}
                >
                  {busyKeys.has(githubUrl.trim())
                    ? t('skills.action.installing')
                    : t('skills.github.install')}
                </Button>
              </form>
            </div>
          ) : null}

          {selectedSkill ? (
            <SkillDetail
              key={skillRowKey(selectedSkill)}
              skill={selectedSkill}
              busyKeys={busyKeys}
              onUse={() => openInChat(skillRowKey(selectedSkill))}
              onUpdate={() => updateMutation.mutate(skillRowKey(selectedSkill))}
              onRemove={() => setPendingRemove(selectedSkill)}
              onInstallDeps={(installId) =>
                depsMutation.mutate({ name: skillRowKey(selectedSkill), installId })
              }
              onSetEnv={(name) => setEnvPrompt({ skill: skillRowKey(selectedSkill), name })}
            />
          ) : selectedItem ? (
            <RegistryDetail
              key={registryKey(selectedItem)}
              item={selectedItem}
              action={installAction(selectedItem, forceArmed)}
              busy={busyKeys.has(registryKey(selectedItem))}
              onInstall={(force) => runInstall(selectedItem, force)}
              mark={<RegistryMark item={selectedItem} size="lg" />}
            />
          ) : (
            <div className="sk-detail__empty">
              <Sparkles className="size-8 text-dim" strokeWidth={1.25} aria-hidden />
              <p>
                {registryLoading || skillsQuery.isPending
                  ? t('skills.list.loading')
                  : t('skills.detail.select')}
              </p>
            </div>
          )}
        </section>
      </div>

      {envPrompt ? (
        <SetEnvSheet
          key={envPrompt.skill + ':' + envPrompt.name}
          name={envPrompt.name}
          saving={envSaving}
          onSubmit={(value) => void saveEnv(value)}
          onClose={() => {
            if (!envSaving) setEnvPrompt(null)
          }}
        />
      ) : null}

      {pendingRemove ? (
        <RemoveConfirm
          skill={pendingRemove}
          busy={uninstallMutation.isPending}
          onCancel={() => setPendingRemove(null)}
          onConfirm={() =>
            uninstallMutation.mutate({
              name: skillRowKey(pendingRemove),
              identifier: pendingRemove.acquisition?.identifier || '',
            })
          }
        />
      ) : null}
    </>
  )
}

/**
 * The strip above a partner catalog: the brand, what the catalog is, and the
 * prerequisite it depends on. Rendered as a note so it is read before the
 * user installs anything it applies to.
 */
function SourceIntro({
  brand,
  title,
  description,
  notice,
  count,
}: {
  brand: 'robinhood' | 'bankr' | 'aeon' | 'capminal'
  title: string
  description: string
  notice?: string
  count: number
}) {
  return (
    <div className="sk-intro" data-brand={brand}>
      <BrandMark brand={brand} size="md" />
      <div className="sk-intro__copy">
        <h2>
          {title}
          <span className="sk-intro__count">
            {count} {count === 1 ? tw('skills.skillWord') : tw('skills.skillsWord')}
          </span>
        </h2>
        <p>{description}</p>
        {notice ? (
          <p className="sk-intro__notice" role="note">
            <TriangleAlert className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
            <span>
              <strong>{t('skills.detail.important')}</strong> {notice}
            </span>
          </p>
        ) : null}
      </div>
    </div>
  )
}
