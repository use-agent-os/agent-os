import './skills.css'
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DownloadIcon, RefreshCwIcon, SearchIcon } from 'lucide-react'
import { toast } from 'sonner'
import { AsciiField } from '@/components/AsciiField'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import {
  CAT_LABEL,
  REGISTRY_SEARCH_DEBOUNCE_MS,
  categoryChips,
  communityFilter,
  filterRegistry,
  filterSkills,
  firstUpdateResult,
  groupSkillsByLayer,
  initials,
  installAction,
  installSource,
  installedEmptyMessage,
  layerHelp,
  layerLabel,
  registryEmptyMessage,
  registryKey,
  robinhoodSkills,
  safeUrl,
  skillDotClass,
  skillDotTitle,
  skillStats,
  skillStatus,
  stillMissingCount,
  type DepsInstallResult,
  type RawSkill,
  type RegistryItem,
  type SkillRequirementItem,
  type SkillRequirements,
  type StatusFilter,
  type UpdateResult,
} from './logic'

// skills.js:7 — the Bankr partner tab is shown; the BankrSource backend stays
// wired either way so Bankr skills remain reachable via Community.
const SHOW_BANKR = true

type Tab = 'installed' | 'bankr' | 'robinhood' | 'community'
type RegistryGroup = 'bankr' | 'community'

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

// ── Modal shell (tokenized dialog; overlay + Escape/backdrop close) ──────────
function ModalShell({
  labelledBy,
  onClose,
  children,
}: {
  labelledBy: string
  onClose: () => void
  children: React.ReactNode
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    panelRef.current?.querySelector<HTMLElement>('button, a, input')?.focus()
  }, [])
  return (
    <div
      className="sk-modal__overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        className="sk-modal panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            onClose()
          }
        }}
      >
        {children}
      </div>
    </div>
  )
}

// ── Logo badge (skills.js:643-655) ────────────────────────────────────────────
function LogoBadge({ item, cls }: { item: RegistryItem; cls: string }) {
  const logoUrl = safeUrl(item.logo)
  const [broken, setBroken] = useState(false)
  if (!logoUrl || broken) {
    if (item.emoji && !logoUrl) return <span className={`${cls} ${cls}--emoji`}>{item.emoji}</span>
    return <span className={`${cls} ${cls}--initials`}>{initials(item.provider || item.name)}</span>
  }
  return <img className={cls} src={logoUrl} alt="" loading="lazy" onError={() => setBroken(true)} />
}

// ── Installed skill card (skills.js:447-465) ──────────────────────────────────
function SkillCard({ skill, onOpen }: { skill: RawSkill; onOpen: () => void }) {
  const desc = skill.description || ''
  return (
    <button
      type="button"
      className="sk-card"
      onClick={onOpen}
      aria-label={`Skill ${skill.name}`}
      title={skill.name + (desc ? ': ' + desc : '')}
    >
      <div className="sk-card__head">
        <span className={`sk-card__dot ${skillDotClass(skill)}`} title={skillDotTitle(skill)} />
        {skill.emoji ? <span className="sk-card__emoji">{skill.emoji}</span> : null}
        <span className="sk-card__name">{skill.name}</span>
      </div>
      <p className="sk-card__desc">{desc}</p>
    </button>
  )
}

// ── Registry card (skills.js:657-678) ─────────────────────────────────────────
function RegistryCard({
  item,
  forceArmed,
  busy,
  onOpen,
  onInstall,
}: {
  item: RegistryItem
  forceArmed: Set<string>
  busy: boolean
  onOpen: () => void
  onInstall: (force: boolean) => void
}) {
  const action = installAction(item, forceArmed)
  // skills.js:659-660 — the category chip only shows for a known, non-'other'
  // category (label from CAT_LABEL, falling back to the raw category key).
  const cat = item.category && item.category !== 'other' ? item.category : ''
  return (
    <div
      className="sk-rcard"
      role="button"
      tabIndex={0}
      aria-label={`Catalog skill ${item.name}`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
    >
      <div className="sk-rcard__head">
        <LogoBadge item={item} cls="sk-rcard__logo" />
        <div className="sk-rcard__titles">
          <span className="sk-rcard__name">{item.name}</span>
          <span className="sk-rcard__provider">{item.provider || item.source || ''}</span>
        </div>
        {cat ? <span className="sk-rcard__cat">{CAT_LABEL[cat] || cat}</span> : null}
      </div>
      <p className="sk-rcard__desc">{item.description || 'View details →'}</p>
      <div className="sk-rcard__foot">
        <span className="sk-rcard__src sk-mono">{item.source || ''}</span>
        <InstallButton
          action={action}
          busy={busy}
          onInstall={(force, e) => {
            e.stopPropagation()
            onInstall(force)
          }}
        />
      </div>
    </div>
  )
}

// ── Install button (skills.js:633-641) — per-item busy, force arming ──────────
function InstallButton({
  action,
  busy,
  large,
  onInstall,
}: {
  action: ReturnType<typeof installAction>
  busy: boolean
  large?: boolean
  onInstall: (force: boolean, e: React.MouseEvent) => void
}) {
  if (action === 'installed') return <span className="sk-chip sk-chip--ok">✓ Installed</span>
  if (action === 'force') {
    return (
      <Button
        type="button"
        size={large ? 'default' : 'sm'}
        variant="destructive"
        disabled={busy}
        onClick={(e) => onInstall(true, e)}
      >
        {busy ? 'Force installing…' : '⚠ Force install'}
      </Button>
    )
  }
  return (
    <Button
      type="button"
      size={large ? 'default' : 'sm'}
      disabled={busy}
      onClick={(e) => onInstall(false, e)}
    >
      {busy ? 'Installing…' : large ? 'Install skill' : 'Install'}
    </Button>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
type Dialog =
  | { kind: 'none' }
  | { kind: 'skill'; name: string }
  | { kind: 'registry'; group: RegistryGroup; key: string }

export function SkillsPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()

  const [tab, setTab] = useState<Tab>('installed')
  const [filterText, setFilterText] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [dialog, setDialog] = useState<Dialog>({ kind: 'none' })

  // Registry (bankr/community) query text + debounced community query.
  const [bankrQuery, setBankrQuery] = useState('')
  const [communityText, setCommunityText] = useState('')
  const [communityQuery, setCommunityQuery] = useState('')
  const [bankrCat, setBankrCat] = useState('all')
  const [communityCat, setCommunityCat] = useState('all')
  const [githubUrl, setGithubUrl] = useState('')

  // Force-armed identifiers (skills.js:34) + per-item busy keys.
  const [forceArmed, setForceArmed] = useState<Set<string>>(new Set())
  const [busyKeys, setBusyKeys] = useState<Set<string>>(new Set())

  useEffect(() => {
    document.title = 'Skills - AgentOS Control'
  }, [])

  // skills.js:210-220 — debounce the community search input (250ms). A cleared
  // input drops the query so the snapshot shows again.
  useEffect(() => {
    const id = setTimeout(
      () => setCommunityQuery(communityText.trim()),
      REGISTRY_SEARCH_DEBOUNCE_MS,
    )
    return () => clearTimeout(id)
  }, [communityText])

  // ── Installed skills (skills.js:325-340) ──────────────────────────────────
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
      const err = skillsQuery.error
      toast.error('Failed to load skills: ' + (err instanceof Error ? err.message : String(err)), {
        id: 'skills-load-err',
      })
    }
  }, [skillsQuery.isError, skillsQuery.error])

  // ── Registry snapshots: skills.search per group on tab entry (skills.js:507) ─
  const bankrSnapshot = useQuery<RegistryItem[]>({
    queryKey: ['skills.search', 'bankr'],
    enabled: SHOW_BANKR && tab === 'bankr',
    refetchOnWindowFocus: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<SearchResponse>('skills.search', {
        query: '',
        limit: 500,
        source: 'bankr',
      })
      return data.results ?? []
    },
  })

  const communitySnapshot = useQuery<RegistryItem[]>({
    queryKey: ['skills.search', 'community'],
    enabled: tab === 'community',
    refetchOnWindowFocus: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<SearchResponse>('skills.search', { query: '', limit: 500 })
      return communityFilter(data.results ?? [], SHOW_BANKR)
    },
  })

  // skills.js:528-545 — a non-empty community query hits the server (the
  // snapshot only covers each source's first page). Debounced; stale drops are
  // handled by react-query keying the query text.
  const communitySearch = useQuery<RegistryItem[]>({
    queryKey: ['skills.search', 'community', communityQuery],
    enabled: tab === 'community' && communityQuery.length > 0,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<SearchResponse>('skills.search', {
        query: communityQuery,
        limit: 100,
      })
      return communityFilter(data.results ?? [], SHOW_BANKR)
    },
  })

  const invalidateSkills = () => queryClient.invalidateQueries({ queryKey: ['skills'] })

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

  // ── Mutations ─────────────────────────────────────────────────────────────
  // skills.js:926-977 — install. Per-item busy; a "dangerous" scan verdict
  // arms an explicit force-install override (not an error).
  const installMutation = useMutation({
    mutationFn: (vars: { identifier: string; source: string; force: boolean }) =>
      rpc.call<InstallResponse>('skills.install', vars),
    onMutate: (vars) => setBusy(vars.identifier, true),
    onSettled: (_d, _e, vars) => setBusy(vars.identifier, false),
    onSuccess: (res, vars) => {
      if (res?.success) {
        armForce(vars.identifier, false)
        toast.success('Installed ' + (res.name || vars.identifier), { id: 'skills-install' })
        void invalidateSkills()
        void queryClient.invalidateQueries({ queryKey: ['skills.search'] })
        return
      }
      const blocked = res?.scan_verdict === 'dangerous'
      const n = (res?.scan_findings || []).length
      if (blocked && !vars.force) {
        armForce(vars.identifier, true)
        toast.error(
          `Security scan flagged ${res?.name || 'this skill'}${
            n ? ` (${n} finding${n === 1 ? '' : 's'})` : ''
          }. Click again to install anyway.`,
          { id: 'skills-install-err' },
        )
      } else {
        toast.error(res?.message || 'Install failed', { id: 'skills-install-err' })
      }
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : String(err), { id: 'skills-install-err' })
    },
  })

  // skills.js:979-991 — uninstall (managed skills only). Per-item busy.
  const uninstallMutation = useMutation({
    mutationFn: (name: string) => rpc.call<MutationResponse>('skills.uninstall', { name }),
    onMutate: (name) => setBusy('uninstall:' + name, true),
    onSettled: (_d, _e, name) => setBusy('uninstall:' + name, false),
    onSuccess: (res, name) => {
      if (res?.success) {
        toast.success('Removed ' + name, { id: 'skills-uninstall' })
        setDialog({ kind: 'none' })
        void invalidateSkills()
      } else {
        toast.error(res?.message || 'Uninstall failed', { id: 'skills-uninstall-err' })
      }
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : String(err), { id: 'skills-uninstall-err' })
    },
  })

  // skills.js:993-1010 — update (re-pull latest). Per-item busy. skills.update
  // returns a results[] array.
  const updateMutation = useMutation({
    mutationFn: (name: string) => rpc.call<UpdateResult>('skills.update', { name }),
    onMutate: (name) => setBusy('update:' + name, true),
    onSettled: (_d, _e, name) => setBusy('update:' + name, false),
    onSuccess: (res, name) => {
      const result = firstUpdateResult(res)
      if (result.success) {
        toast.success(result.message || `Updated ${name}`, { id: 'skills-update' })
        void invalidateSkills()
      } else {
        toast.error(result.message || res?.message || 'Update failed', { id: 'skills-update-err' })
      }
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : String(err), { id: 'skills-update-err' })
    },
  })

  // skills.js:879-908 — install a skill's declared dependency (deps.install).
  // Closes the dialog + reloads once nothing is still missing.
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
        toast.success(res.message || 'Installed', { id: 'skills-deps' })
        if (stillMissingCount(res) === 0) setDialog({ kind: 'none' })
      } else {
        toast.error(res?.message || 'Install failed', { id: 'skills-deps-err' })
      }
      void invalidateSkills()
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : String(err), { id: 'skills-deps-err' })
    },
  })

  // ── Derivations ───────────────────────────────────────────────────────────
  const allSkills = skillsQuery.data ?? []
  const stats = skillStats(allSkills)
  const filtered = filterSkills(allSkills, filterText, statusFilter)
  const groups = groupSkillsByLayer(filtered)
  const rhSkills = robinhoodSkills(allSkills)

  const runInstall = (item: RegistryItem, force: boolean) =>
    installMutation.mutate({
      identifier: registryKey(item),
      source: installSource(item),
      force,
    })

  const refresh = () => {
    if (tab === 'bankr') void bankrSnapshot.refetch()
    else if (tab === 'community') {
      void communitySnapshot.refetch()
      if (communityQuery) void communitySearch.refetch()
    } else void invalidateSkills()
  }

  return (
    <div className="sk-stage">
      <header className="sk-stage__header">
        <AsciiField />
        <div className="sk-stage__title-block">
          <span className="t-label">Control · Skills</span>
          <h2 className="t-display">Skills</h2>
          <p className="sk-stage__subtitle">
            Composable agent capabilities — bundled packs, partner catalogs, and the wider
            community.
          </p>
        </div>
        <div className="sk-stage__actions">
          {tab === 'installed' ? (
            <div className="sk-search-wrap">
              <SearchIcon className="sk-search-icon" aria-hidden="true" />
              <input
                type="search"
                className="sk-search-input"
                placeholder="Filter installed…"
                aria-label="Filter installed skills"
                autoComplete="off"
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
              />
            </div>
          ) : null}
          <Button
            variant="outline"
            title="Refresh"
            className="text-xs uppercase tracking-[0.14em]"
            onClick={refresh}
          >
            <RefreshCwIcon />
            <span>Refresh</span>
          </Button>
        </div>
      </header>

      {/* Metric pills → status filter (skills.js:342-367) */}
      <section className="sk-metrics" aria-label="Skills summary">
        <MetricPill
          label="All"
          value={stats.total}
          tone="accent"
          active={statusFilter === 'all'}
          onClick={() => setStatusFilter('all')}
        />
        <MetricPill
          label="Ready"
          value={stats.ready}
          tone="ok"
          active={statusFilter === 'ready'}
          onClick={() => setStatusFilter('ready')}
        />
        <MetricPill
          label="Needs setup"
          value={stats.needs}
          tone="warn"
          active={statusFilter === 'needs-setup'}
          onClick={() => setStatusFilter('needs-setup')}
        />
        <MetricPill
          label="No manifest"
          value={stats.notDeclared}
          active={statusFilter === 'not-declared'}
          onClick={() => setStatusFilter('not-declared')}
        />
      </section>

      {/* Tabs (skills.js:107-112) */}
      <div className="sk-tabs" role="group" aria-label="Skill source">
        <TabButton current={tab} tab="installed" onSelect={setTab}>
          Installed
        </TabButton>
        {SHOW_BANKR ? (
          <TabButton current={tab} tab="bankr" onSelect={setTab}>
            Bankr
          </TabButton>
        ) : null}
        <TabButton current={tab} tab="robinhood" onSelect={setTab}>
          Robinhood
        </TabButton>
        <TabButton current={tab} tab="community" onSelect={setTab}>
          Community
        </TabButton>
      </div>

      {tab === 'installed' ? (
        <InstalledPanel
          loading={skillsQuery.isLoading}
          error={skillsQuery.isError ? String(skillsQuery.error) : ''}
          groups={groups}
          empty={filtered.length === 0}
          emptyMessage={installedEmptyMessage(filterText, statusFilter)}
          onOpen={(name) => setDialog({ kind: 'skill', name })}
        />
      ) : null}

      {SHOW_BANKR && tab === 'bankr' ? (
        <RegistryPanel
          group="bankr"
          snapshot={bankrSnapshot.data ?? []}
          loading={bankrSnapshot.isLoading}
          error={bankrSnapshot.isError ? String(bankrSnapshot.error) : ''}
          query={bankrQuery}
          onQuery={setBankrQuery}
          category={bankrCat}
          onCategory={setBankrCat}
          forceArmed={forceArmed}
          busyKeys={busyKeys}
          onOpen={(key) => setDialog({ kind: 'registry', group: 'bankr', key })}
          onInstall={runInstall}
        />
      ) : null}

      {tab === 'robinhood' ? (
        <RobinhoodPanel skills={rhSkills} onOpen={(name) => setDialog({ kind: 'skill', name })} />
      ) : null}

      {tab === 'community' ? (
        <>
          <div className="sk-github-install">
            <div className="sk-search-wrap sk-search-wrap--lg">
              <DownloadIcon className="sk-search-icon" aria-hidden="true" />
              <input
                type="url"
                className="sk-search-input sk-search-input--lg"
                placeholder="https://github.com/owner/repo/tree/main/path/to/skill"
                aria-label="Install from GitHub URL"
                autoComplete="off"
                value={githubUrl}
                onChange={(e) => setGithubUrl(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && githubUrl.trim()) {
                    installMutation.mutate({
                      identifier: githubUrl.trim(),
                      source: 'github',
                      force: false,
                    })
                  }
                }}
              />
            </div>
            <Button
              onClick={() => {
                if (githubUrl.trim())
                  installMutation.mutate({
                    identifier: githubUrl.trim(),
                    source: 'github',
                    force: false,
                  })
              }}
            >
              Install GitHub URL
            </Button>
          </div>
          <RegistryPanel
            group="community"
            // A live query supersedes the snapshot as the base list.
            snapshot={
              communityQuery && communitySearch.data
                ? communitySearch.data
                : (communitySnapshot.data ?? [])
            }
            chipSnapshot={communitySnapshot.data ?? []}
            loading={communityQuery ? communitySearch.isLoading : communitySnapshot.isLoading}
            error={communitySnapshot.isError ? String(communitySnapshot.error) : ''}
            query={communityText}
            onQuery={setCommunityText}
            category={communityCat}
            onCategory={setCommunityCat}
            forceArmed={forceArmed}
            busyKeys={busyKeys}
            onOpen={(key) => setDialog({ kind: 'registry', group: 'community', key })}
            onInstall={runInstall}
          />
        </>
      ) : null}

      {dialog.kind === 'skill'
        ? (() => {
            const skill = allSkills.find((s) => s.name === dialog.name)
            if (!skill) return null
            return (
              <SkillDialog
                skill={skill}
                busyKeys={busyKeys}
                onClose={() => setDialog({ kind: 'none' })}
                onUpdate={() => updateMutation.mutate(skill.name!)}
                onRemove={() => uninstallMutation.mutate(skill.name!)}
                onInstallDeps={(installId) => depsMutation.mutate({ name: skill.name!, installId })}
              />
            )
          })()
        : null}

      {dialog.kind === 'registry'
        ? (() => {
            const base =
              dialog.group === 'bankr'
                ? (bankrSnapshot.data ?? [])
                : communityQuery && communitySearch.data
                  ? communitySearch.data
                  : (communitySnapshot.data ?? [])
            const item = base.find((r) => registryKey(r) === dialog.key)
            if (!item) return null
            return (
              <RegistryDialog
                item={item}
                forceArmed={forceArmed}
                busy={busyKeys.has(registryKey(item))}
                onClose={() => setDialog({ kind: 'none' })}
                onInstall={(force) => runInstall(item, force)}
              />
            )
          })()
        : null}
    </div>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

function MetricPill({
  label,
  value,
  tone,
  active,
  onClick,
}: {
  label: string
  value: number
  tone?: 'accent' | 'ok' | 'warn'
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      className={`sk-metric${tone ? ' sk-metric--' + tone : ''}${active ? ' is-active' : ''}`}
      title={`Filter: ${label}`}
      aria-label={`Filter: ${label}`}
      aria-pressed={active}
      onClick={onClick}
    >
      <span className="sk-metric__value">{value}</span>
      <span className="sk-metric__label">{label}</span>
    </button>
  )
}

function TabButton({
  current,
  tab,
  onSelect,
  children,
}: {
  current: Tab
  tab: Tab
  onSelect: (t: Tab) => void
  children: React.ReactNode
}) {
  const active = current === tab
  return (
    <button
      type="button"
      className={`sk-tab${active ? ' is-active' : ''}`}
      aria-pressed={active}
      onClick={() => onSelect(tab)}
    >
      {children}
    </button>
  )
}

function InstalledPanel({
  loading,
  error,
  groups,
  empty,
  emptyMessage,
  onOpen,
}: {
  loading: boolean
  error: string
  groups: ReturnType<typeof groupSkillsByLayer>
  empty: boolean
  emptyMessage: string
  onOpen: (name: string) => void
}) {
  if (error) return <div className="sk-error">Failed to load skills: {error}</div>
  if (loading) return <div className="sk-registry__loading">Loading skills…</div>
  if (empty) return <div className="sk-empty__state">{emptyMessage}</div>
  return (
    <div className="sk-panel">
      {groups.map((g) => (
        <details key={g.layer} className="sk-group" open>
          <summary className="sk-group__head">
            <span className="sk-group__caret" aria-hidden="true">
              ▾
            </span>
            <span className="sk-group__label">{g.label}</span>
            <span className="sk-group__count">{g.skills.length}</span>
            <span className="sk-group__meta">{layerHelp(g.layer)}</span>
          </summary>
          <div className="sk-grid">
            {g.skills.map((s) => (
              <SkillCard key={s.name} skill={s} onOpen={() => onOpen(s.name!)} />
            ))}
          </div>
        </details>
      ))}
    </div>
  )
}

function RobinhoodPanel({
  skills,
  onOpen,
}: {
  skills: RawSkill[]
  onOpen: (name: string) => void
}) {
  if (skills.length === 0) {
    return (
      <div className="sk-empty">
        <p className="sk-empty__title">Robinhood skills are on the way</p>
        <p className="sk-empty__hint">
          No Robinhood skills installed yet. Check back soon, or browse the Bankr &amp; Community
          catalogs in the meantime.
        </p>
      </div>
    )
  }
  return (
    <div className="sk-panel">
      <div className="sk-grid">
        {skills.map((s) => (
          <SkillCard key={s.name} skill={s} onOpen={() => onOpen(s.name!)} />
        ))}
      </div>
    </div>
  )
}

function RegistryPanel({
  group,
  snapshot,
  chipSnapshot,
  loading,
  error,
  query,
  onQuery,
  category,
  onCategory,
  forceArmed,
  busyKeys,
  onOpen,
  onInstall,
}: {
  group: RegistryGroup
  snapshot: RegistryItem[]
  chipSnapshot?: RegistryItem[]
  loading: boolean
  error: string
  query: string
  onQuery: (q: string) => void
  category: string
  onCategory: (c: string) => void
  forceArmed: Set<string>
  busyKeys: Set<string>
  onOpen: (key: string) => void
  onInstall: (item: RegistryItem, force: boolean) => void
}) {
  // skills.js:567 — chips derive from the FULL snapshot only.
  const chips = useMemo(
    () => categoryChips(chipSnapshot ?? snapshot, category),
    [chipSnapshot, snapshot, category],
  )
  // skills.js:610-620 — apply category then text filter.
  const items = useMemo(
    () => filterRegistry(snapshot, category, query),
    [snapshot, category, query],
  )

  return (
    <div className="sk-panel">
      <div className="sk-browse__bar">
        <div className="sk-search-wrap sk-search-wrap--lg">
          <SearchIcon className="sk-search-icon" aria-hidden="true" />
          <input
            type="search"
            className="sk-search-input sk-search-input--lg"
            placeholder={group === 'bankr' ? 'Search Bankr skills…' : 'Search community skills…'}
            aria-label={group === 'bankr' ? 'Search Bankr skills' : 'Search community skills'}
            autoComplete="off"
            value={query}
            onChange={(e) => onQuery(e.target.value)}
          />
        </div>
      </div>
      {chips.length ? (
        <div className="sk-chips">
          {chips.map((c) => (
            <button
              key={c.cat}
              type="button"
              className={`sk-chip-btn${c.active ? ' is-active' : ''}`}
              onClick={() => onCategory(c.cat)}
            >
              {c.label} <span className="sk-chip-btn__count">{c.count}</span>
            </button>
          ))}
        </div>
      ) : null}
      <div className="sk-browse__results">
        {error ? (
          <div className="sk-error">
            Failed to load: {error}
            <br />
            <span className="sk-dim">Re-open the tab or press Refresh to retry.</span>
          </div>
        ) : loading ? (
          <div className="sk-registry__loading">
            {group === 'bankr' ? 'Loading Bankr catalog…' : 'Loading community catalog…'}
          </div>
        ) : items.length === 0 ? (
          <div className="sk-registry__hint">{registryEmptyMessage(group, query)}</div>
        ) : (
          <div className="sk-grid sk-grid--registry">
            {items.map((r) => (
              <RegistryCard
                key={registryKey(r)}
                item={r}
                forceArmed={forceArmed}
                busy={busyKeys.has(registryKey(r))}
                onOpen={() => onOpen(registryKey(r))}
                onInstall={(force) => onInstall(r, force)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Requirements section (skills.js:743-777) ──────────────────────────────────
// Per-requirement name + status chip + missing/requires detail. Renders nothing
// when there are no requirement items.
function reqStatusLabel(status: string): string {
  if (status === 'ready') return 'ready'
  if (status === 'needs_setup') return 'needs setup'
  if (status === 'missing_skill') return 'missing skill'
  return 'no deps declared'
}

function reqStatusClass(status: string): string {
  if (status === 'ready') return 'sk-chip--ok'
  if (status === 'needs_setup' || status === 'missing_skill') return 'sk-chip--warn'
  return 'sk-chip--unverified'
}

function RequirementRow({ item }: { item: SkillRequirementItem }) {
  // skills.js:747-749 — missing bins + env, each rendered as <code>.
  const missing = [...(item.missing_bins || []), ...(item.missing_env || [])]
  // skills.js:750-755 — declared requirements as plain text fragments.
  const requires: string[] = [...(item.requires_bins || [])]
  if ((item.requires_any_bins || []).length) {
    requires.push(`one of ${(item.requires_any_bins || []).join(' / ')}`)
  }
  ;(item.requires_env || []).forEach((e) => requires.push(`${e} env`))
  const status = item.status || 'not_declared'

  // skills.js:764-766 — detail prefers the missing codes, else the requires
  // text, else a fallback string.
  let detail: React.ReactNode
  if (missing.length) {
    detail = (
      <>
        Missing{' '}
        {missing.map((m, i) => (
          <span key={m}>
            {i > 0 ? ', ' : ''}
            <code>{m}</code>
          </span>
        ))}
      </>
    )
  } else if (requires.length) {
    detail = requires.join(', ')
  } else {
    detail = 'No declared dependencies'
  }

  return (
    <div className="sk-dialog__req-row">
      <span className="sk-dialog__req-name">{item.name || 'unknown'}</span>
      <span className={`sk-chip ${reqStatusClass(status)}`}>{reqStatusLabel(status)}</span>
      <span className="sk-dialog__req-detail">{detail}</span>
    </div>
  )
}

function RequirementsSection({ requirements }: { requirements?: SkillRequirements }) {
  const items = Array.isArray(requirements?.items) ? requirements.items : []
  if (!items.length) return null
  return (
    <div className="sk-dialog__section">
      <div className="sk-dialog__section-title">Requirements</div>
      <div className="sk-dialog__requirements">
        {items.map((item, i) => (
          <RequirementRow key={item.name || i} item={item} />
        ))}
      </div>
    </div>
  )
}

// ── Installed skill detail dialog (skills.js:779-864) ─────────────────────────
function SkillDialog({
  skill,
  busyKeys,
  onClose,
  onUpdate,
  onRemove,
  onInstallDeps,
}: {
  skill: RawSkill
  busyKeys: Set<string>
  onClose: () => void
  onUpdate: () => void
  onRemove: () => void
  onInstallDeps: (installId: string) => void
}) {
  const titleId = useId()
  const status = skillStatus(skill)
  const isManaged = skill.layer === 'managed'
  const hasMissingBins = (skill.missing_bins || []).length > 0
  const installs = hasMissingBins ? skill.install || [] : []
  const homepage = safeUrl(skill.homepage)
  const updateBusy = busyKeys.has('update:' + skill.name)
  const removeBusy = busyKeys.has('uninstall:' + skill.name)

  // skills.js:792-803 — the Missing bins/env list only shows for needs_setup.
  const missingBins = status === 'needs_setup' ? skill.missing_bins || [] : []
  const missingEnv = status === 'needs_setup' ? skill.missing_env || [] : []
  const hasMissing = missingBins.length > 0 || missingEnv.length > 0

  return (
    <ModalShell labelledBy={titleId} onClose={onClose}>
      <header className="sk-dialog__head">
        <div className="sk-dialog__head-left">
          {skill.emoji ? <span className="sk-dialog__emoji">{skill.emoji}</span> : null}
          <strong id={titleId} className="sk-dialog__name">
            {skill.name}
          </strong>
          <div className="sk-dialog__chips">
            <span className="sk-chip" title={layerHelp(skill.layer)}>
              {layerLabel(skill.layer)}
            </span>
            {status === 'ready' ? (
              <span className="sk-chip sk-chip--ok">✓ ready</span>
            ) : status === 'not_declared' ? (
              <span className="sk-chip sk-chip--unverified">no deps declared</span>
            ) : (
              <span className="sk-chip sk-chip--warn">needs deps</span>
            )}
          </div>
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={onClose} aria-label="Close">
          ✕
        </Button>
      </header>
      <section className="sk-dialog__body">
        <p className="sk-dialog__desc">{skill.description || ''}</p>
        <RequirementsSection requirements={skill.requirements} />
        {hasMissing ? (
          <div className="sk-dialog__section">
            <div className="sk-dialog__section-title">Missing</div>
            <ul className="sk-dialog__missing">
              {missingBins.map((b) => (
                <li key={`bin:${b}`}>
                  <code>{b}</code> <span className="sk-dim">binary</span>
                </li>
              ))}
              {missingEnv.map((e) => (
                <li key={`env:${e}`}>
                  <code>{e}</code> <span className="sk-dim">env var</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {installs.length ? (
          <div className="sk-dialog__section">
            <div className="sk-dialog__section-title">Install</div>
            {installs.map((i) => {
              const busy = busyKeys.has('deps:' + skill.name + ':' + i.id)
              return (
                <div key={i.id} className="sk-dialog__install-row">
                  <span>
                    {i.label || `Install via ${i.kind}`}
                    {(i.bins || []).length ? (
                      <span className="sk-dim"> ({(i.bins || []).join(', ')})</span>
                    ) : null}
                  </span>
                  <Button
                    type="button"
                    size="sm"
                    disabled={busy}
                    onClick={() => onInstallDeps(i.id!)}
                  >
                    {busy ? 'Installing…' : `Install via ${i.kind}`}
                  </Button>
                </div>
              )
            })}
          </div>
        ) : null}
        {homepage ? (
          <div className="sk-dialog__section">
            <a href={homepage} target="_blank" rel="noopener" className="sk-dialog__link">
              Homepage ↗
            </a>
          </div>
        ) : null}
      </section>
      <footer className="sk-dialog__foot">
        {skill.file_path ? (
          <small className="sk-dim sk-dialog__path">{skill.file_path}</small>
        ) : null}
        {isManaged ? (
          <>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={updateBusy}
              onClick={onUpdate}
            >
              {updateBusy ? 'Updating…' : 'Update'}
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={removeBusy}
              onClick={onRemove}
            >
              {removeBusy ? 'Removing…' : 'Remove'}
            </Button>
          </>
        ) : null}
      </footer>
    </ModalShell>
  )
}

// ── Registry detail dialog (skills.js:680-739) ────────────────────────────────
function RegistryDialog({
  item,
  forceArmed,
  busy,
  onClose,
  onInstall,
}: {
  item: RegistryItem
  forceArmed: Set<string>
  busy: boolean
  onClose: () => void
  onInstall: (force: boolean) => void
}) {
  const titleId = useId()
  const homepage = safeUrl(item.homepage)
  const action = installAction(item, forceArmed)
  // skills.js:685 — category chip between trust and source (known, non-'other').
  const cat = item.category && item.category !== 'other' ? item.category : ''
  // skills.js:703-704 — demo section heading appends the demo title + language.
  const demoTitle = item.demo?.title || ''
  const demoLang = item.demo?.language || ''
  return (
    <ModalShell labelledBy={titleId} onClose={onClose}>
      <header className="sk-dialog__head">
        <div className="sk-dialog__head-left">
          <LogoBadge item={item} cls="sk-dialog__logo" />
          <div>
            <strong id={titleId} className="sk-dialog__name">
              {item.name}
            </strong>
            <div className="sk-dialog__provider">{item.provider || ''}</div>
          </div>
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={onClose} aria-label="Close">
          ✕
        </Button>
      </header>
      <section className="sk-dialog__body">
        <div className="sk-dialog__chips">
          <span
            className={`sk-chip ${item.trust_level === 'trusted' ? 'sk-chip--ok' : 'sk-chip--warn'}`}
          >
            {item.trust_level || 'community'}
          </span>
          {cat ? <span className="sk-chip">{CAT_LABEL[cat] || cat}</span> : null}
          <span className="sk-chip sk-mono">{item.source || ''}</span>
        </div>
        {item.description ? (
          <p className="sk-dialog__desc">{item.description}</p>
        ) : (
          <p className="sk-dialog__desc sk-dim">
            Description loads after install (from the skill&apos;s SKILL.md).
          </p>
        )}
        {Array.isArray(item.setup) && item.setup.length ? (
          <div className="sk-dialog__section">
            <div className="sk-dialog__section-title">Setup</div>
            <ol className="sk-dialog__setup">
              {item.setup.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </div>
        ) : null}
        {item.demo && item.demo.code ? (
          <div className="sk-dialog__section">
            <div className="sk-dialog__section-title">
              Demo{' '}
              {demoTitle ? (
                <span className="sk-dialog__demo-title sk-mono">{demoTitle}</span>
              ) : null}{' '}
              {demoLang ? <span className="sk-dialog__demo-lang sk-mono">{demoLang}</span> : null}
            </div>
            <pre className="sk-dialog__code">
              <code>{item.demo.code}</code>
            </pre>
          </div>
        ) : null}
        {homepage ? (
          <div className="sk-dialog__section">
            <a href={homepage} target="_blank" rel="noopener" className="sk-dialog__link">
              Source ↗
            </a>
          </div>
        ) : null}
      </section>
      <footer className="sk-dialog__foot">
        <small className="sk-dim sk-mono sk-dialog__path">{registryKey(item)}</small>
        <InstallButton action={action} busy={busy} large onInstall={(force) => onInstall(force)} />
      </footer>
    </ModalShell>
  )
}
