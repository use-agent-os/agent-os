import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check,
  Download,
  Eye,
  EyeOff,
  Globe,
  MessageSquare,
  Package,
  RefreshCw,
  Search,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import aeonSymbolUrl from '@/assets/aeon-symbol.png'
import bankrSymbolUrl from '@/assets/bankr-symbol.svg'
import capminalSymbolUrl from '@/assets/capminal-symbol.svg'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webCommon'
import '@/i18n/en/webSetCapabilities'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  catLabel,
  categoryChips,
  communityFilter,
  filterRegistry,
  filterSkills,
  firstUpdateResult,
  groupSkills,
  installAction,
  installedEmptyMessage,
  installSource,
  layerHelp,
  layerLabel,
  markInstalled,
  registryEmptyMessage,
  registryKey,
  REGISTRY_SEARCH_DEBOUNCE_MS,
  safeUrl,
  skillAvailabilityLabel,
  skillAvailabilityTitle,
  skillAvailabilityTone,
  skillBucket,
  skillBucketLabel,
  skillCanRemove,
  skillCanUpdate,
  skillChatPromptPath,
  skillDotTitle,
  skillPublisherId,
  skillStats,
  skillStatus,
  stillMissingCount,
  type DepsInstallResult,
  type RawSkill,
  type RegistryItem,
  type SkillBucket,
  type StatusFilter,
  type UpdateResult,
} from '@/views/skills/logic'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/web/components/ui/alert-dialog'
import { Button } from '@/web/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/web/components/ui/dialog'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/web/components/ui/tabs'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { useRegisterFields, useReveal } from '../fields'

// Settings › Skills (control-ui-settings "Skills"): installed skills grouped by
// publisher, the partner catalogs (Bankr, Aeon, Capminal) and the community
// hub, on the kept `views/skills/logic.ts` derivations. Every action that
// sends an RPC is gated on the displayed connection and says why when blocked.

type Partner = 'bankr' | 'aeon' | 'capminal'
type CatalogSource = Partner | 'community'
type Tab = 'installed' | CatalogSource

const PARTNERS: readonly Partner[] = ['bankr', 'aeon', 'capminal']
const PARTNER_MARKS: Record<Partner, string> = {
  bankr: bankrSymbolUrl,
  aeon: aeonSymbolUrl,
  capminal: capminalSymbolUrl,
}
/** Partner catalogs own a tab, so their rows stay out of Community. */
const PARTNER_SOURCES: ReadonlySet<string> = new Set(PARTNERS)

const inputClass =
  'h-9 w-full rounded-md border border-border-control bg-surface-0 px-3 touch:h-11 touch:text-base'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function isPartner(id: string): id is Partner {
  return (PARTNERS as readonly string[]).includes(id)
}

function partnerCopy(partner: Partner) {
  return {
    bankr: {
      name: t('webSetCapabilities.skillsPartnerBankr'),
      title: t('webSetCapabilities.skillsBankrTitle'),
      description: t('webSetCapabilities.skillsBankrBody'),
      notice: t('webSetCapabilities.skillsBankrNotice'),
    },
    aeon: {
      name: t('webSetCapabilities.skillsPartnerAeon'),
      title: t('webSetCapabilities.skillsAeonTitle'),
      description: t('webSetCapabilities.skillsAeonBody'),
      notice: t('webSetCapabilities.skillsAeonNotice'),
    },
    capminal: {
      name: t('webSetCapabilities.skillsPartnerCapminal'),
      title: t('webSetCapabilities.skillsCapminalTitle'),
      description: t('webSetCapabilities.skillsCapminalBody'),
      notice: t('webSetCapabilities.skillsCapminalNotice'),
    },
  }[partner]
}

function PartnerMark({ partner, className }: { partner: Partner; className?: string }) {
  return (
    <img
      src={PARTNER_MARKS[partner]}
      alt={t('webSetCapabilities.skillsPartnerLogo', { name: partnerCopy(partner).name })}
      width={32}
      height={32}
      className={cn('size-8 shrink-0 rounded-md object-contain', className)}
    />
  )
}

/** The block reason for every action in this section, referenced by `aria-describedby`. */
function GateNotice({ id, blocked }: { id: string; blocked: Gate }) {
  if (!blocked.reason) return null
  return (
    <p
      id={id}
      role="status"
      className="mb-3 flex items-center gap-2 rounded-md border border-warn/50 bg-warn/10 px-3 py-2 text-small"
    >
      <TriangleAlert className="size-4 shrink-0 text-warn" aria-hidden data-status-icon />
      {blocked.reason}
    </p>
  )
}

const BUCKET_TONE: Record<SkillBucket, string> = {
  ready: 'border-ok/40 text-ok',
  'needs-setup': 'border-warn/50 text-warn',
  disabled: 'border-border text-text-muted',
}

function Chip({ className, title, children }: React.ComponentProps<'span'>) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-small whitespace-nowrap',
        className,
      )}
    >
      {children}
    </span>
  )
}

function StatusChip({ skill }: { skill: RawSkill }) {
  const bucket = skillBucket(skill)
  return (
    <Chip className={BUCKET_TONE[bucket]} title={skillDotTitle(skill)}>
      {skillBucketLabel(bucket)}
    </Chip>
  )
}

/** Installed and ready is not the same as offered to the agent; say so when it is not. */
function AvailabilityChip({ skill }: { skill: RawSkill }) {
  if (skillAvailabilityTone(skill) !== 'not-offered') return null
  return (
    <Chip className="border-warn/50 text-warn" title={skillAvailabilityTitle(skill)}>
      {skillAvailabilityLabel(skill)}
    </Chip>
  )
}

// ── Data and actions ────────────────────────────────────────────────────────

interface InstallResponse {
  success?: boolean
  name?: string
  message?: string
  scan_verdict?: string
  scan_findings?: unknown[]
  overridable?: boolean
}

function useInstalledSkills() {
  const rpc = useRpc()
  return useQuery({
    queryKey: ['skills', 'list'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<{ skills?: RawSkill[] }>('skills.list', {})
      return data?.skills ?? []
    },
  })
}

function useCatalog(source: CatalogSource, query: string, enabled: boolean) {
  const rpc = useRpc()
  return useQuery({
    queryKey: ['skills', 'search', source, query],
    enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<{ results?: RegistryItem[] }>('skills.search', {
        query,
        limit: query ? 100 : 500,
        ...(source === 'community' ? {} : { source }),
      })
      const rows = data?.results ?? []
      return source === 'community' ? communityFilter(rows, PARTNER_SOURCES) : rows
    },
  })
}

function toggled(set: ReadonlySet<string>, key: string, on: boolean): ReadonlySet<string> {
  const next = new Set(set)
  if (on) next.add(key)
  else next.delete(key)
  return next
}

/** Install, uninstall, update, dependency install and env writes, with per-item busy keys. */
function useSkillActions() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set())
  // A security-scan block arms an explicit "Install anyway" on that row.
  const [armed, setArmed] = useState<ReadonlySet<string>>(new Set())

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['skills'] })
  const markCached = (identifier: string, name: string, installed: boolean) =>
    queryClient.setQueriesData<RegistryItem[]>({ queryKey: ['skills', 'search'] }, (old) =>
      old ? markInstalled(old, identifier, name, installed) : old,
    )

  async function run<T>(key: string, work: () => Promise<T>): Promise<T | undefined> {
    setBusy((s) => toggled(s, key, true))
    try {
      return await work()
    } catch (error) {
      toast.error(errorMessage(error))
      return undefined
    } finally {
      setBusy((s) => toggled(s, key, false))
    }
  }

  const install = (identifier: string, source: string, force: boolean) =>
    run(identifier, async () => {
      const res = await rpc.call<InstallResponse>('skills.install', { identifier, source, force })
      if (res?.success) {
        setArmed((s) => toggled(s, identifier, false))
        toast.success(t('webSetCapabilities.skillsInstalled', { name: res.name || identifier }))
        markCached(identifier, res.name || '', true)
        void refresh()
        return true
      }
      const flagged = res?.scan_verdict === 'dangerous' || res?.overridable === true
      if (flagged && !force) {
        setArmed((s) => toggled(s, identifier, true))
        toast.error(
          tPlural('webSetCapabilities.skillsScanBlocked', (res?.scan_findings ?? []).length, {
            name: res?.name || identifier,
          }),
        )
      } else {
        toast.error(res?.message || t('webSetCapabilities.skillsInstallFailed'))
      }
      return false
    })

  const uninstall = (skill: RawSkill) =>
    run(`uninstall:${skill.name}`, async () => {
      const name = String(skill.name)
      const res = await rpc.call<{ success?: boolean; message?: string }>('skills.uninstall', {
        name,
      })
      if (!res?.success) throw new Error(res?.message || t('webSetCapabilities.skillsRemoveFailed'))
      toast.success(t('webSetCapabilities.skillsRemoved', { name }))
      markCached(skill.acquisition?.identifier || '', name, false)
      void refresh()
    })

  const update = (name: string) =>
    run(`update:${name}`, async () => {
      const res = await rpc.call<UpdateResult>('skills.update', { name })
      const result = firstUpdateResult(res)
      if (!result.success) {
        throw new Error(
          result.message || res?.message || t('webSetCapabilities.skillsUpdateFailed'),
        )
      }
      toast.success(result.message || t('webSetCapabilities.skillsUpdated', { name }))
      void refresh()
    })

  const installDeps = (name: string, installId: string) =>
    run(`deps:${name}:${installId}`, async () => {
      const res = await rpc.call<DepsInstallResult>('skills.deps.install', {
        name,
        install_id: installId,
      })
      void refresh()
      if (!res?.success)
        throw new Error(res?.message || t('webSetCapabilities.skillsInstallFailed'))
      toast.success(res.message || t('webSetCapabilities.skillsDepsInstalled'))
      return stillMissingCount(res)
    })

  const setEnv = (name: string, value: string) =>
    run(`env:${name}`, async () => {
      await rpc.call('env.set', { name, value })
      toast.success(t('webSetCapabilities.skillsEnvSaved', { name }))
      await refresh()
      return true
    })

  return { busy, armed, install, uninstall, update, installDeps, setEnv, refresh }
}

type SkillActions = ReturnType<typeof useSkillActions>

// ── Section ─────────────────────────────────────────────────────────────────

export function SkillsSection() {
  const navigate = useNavigate()
  const connection = useDisplayedConnection()
  const blocked = gate(requireConnection(connection))
  const reasonId = useId()
  const actions = useSkillActions()
  const installed = useInstalledSkills()
  const [tab, setTab] = useState<Tab>('installed')
  const [open, setOpen] = useState<string | null>(null)
  const [confirmRemove, setConfirmRemove] = useState<RawSkill | null>(null)

  useRegisterFields('skills', [
    { id: 'skills-installed', label: t('webSetCapabilities.skillsTabInstalled') },
    { id: 'skills-community', label: t('webSetCapabilities.skillsTabCommunity') },
  ])

  const skills = installed.data ?? []
  const openSkill = open ? skills.find((s) => s.name === open) : undefined

  return (
    <div className="flex flex-col gap-4">
      <GateNotice id={reasonId} blocked={blocked} />
      <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
        <div className="flex flex-wrap items-center gap-2">
          <TabsList aria-label={t('webSetCapabilities.skillsTabs')} className="flex-wrap">
            <TabsTrigger value="installed" data-field-id="skills-installed">
              <Package className="size-4" aria-hidden />
              {t('webSetCapabilities.skillsTabInstalled')}
            </TabsTrigger>
            {PARTNERS.map((p) => (
              <TabsTrigger key={p} value={p}>
                <img src={PARTNER_MARKS[p]} alt="" aria-hidden className="size-4" />
                {partnerCopy(p).name}
              </TabsTrigger>
            ))}
            <TabsTrigger value="community" data-field-id="skills-community">
              <Globe className="size-4" aria-hidden />
              {t('webSetCapabilities.skillsTabCommunity')}
            </TabsTrigger>
          </TabsList>
          <span className="flex-1" />
          <Button size="sm" variant="ghost" onClick={() => void actions.refresh()}>
            <RefreshCw aria-hidden />
            {t('webSetCapabilities.refresh')}
          </Button>
        </div>

        <TabsContent value="installed" className="mt-4">
          <InstalledPanel
            skills={skills}
            loading={installed.isPending}
            error={installed.isError ? errorMessage(installed.error) : null}
            onRetry={() => void installed.refetch()}
            onOpen={setOpen}
          />
        </TabsContent>
        {PARTNERS.map((p) => (
          <TabsContent key={p} value={p} className="mt-4">
            {tab === p ? (
              <CatalogPanel source={p} actions={actions} blocked={blocked} reasonId={reasonId} />
            ) : null}
          </TabsContent>
        ))}
        <TabsContent value="community" className="mt-4">
          {tab === 'community' ? (
            <CatalogPanel
              source="community"
              actions={actions}
              blocked={blocked}
              reasonId={reasonId}
            />
          ) : null}
        </TabsContent>
      </Tabs>

      <SkillDialog
        skill={openSkill}
        actions={actions}
        blocked={blocked}
        reasonId={reasonId}
        onClose={() => setOpen(null)}
        onUse={(name) => {
          setOpen(null)
          navigate(skillChatPromptPath(name))
        }}
        onRemove={(skill) => {
          setOpen(null)
          setConfirmRemove(skill)
        }}
      />

      <AlertDialog
        open={confirmRemove !== null}
        onOpenChange={(next) => (next ? undefined : setConfirmRemove(null))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('webSetCapabilities.skillsRemoveTitle', { name: String(confirmRemove?.name) })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('webSetCapabilities.skillsRemoveBody')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('webCommon.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              destructive
              disabled={blocked.disabled}
              onClick={() => {
                if (confirmRemove) void actions.uninstall(confirmRemove)
              }}
            >
              {t('webSetCapabilities.skillsRemove')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

// ── Installed ───────────────────────────────────────────────────────────────

function InstalledPanel({
  skills,
  loading,
  error,
  onRetry,
  onOpen,
}: {
  skills: RawSkill[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onOpen: (name: string) => void
}) {
  const [text, setText] = useState('')
  const [status, setStatus] = useState<StatusFilter>('all')

  if (loading) {
    return (
      <LoadingRegion>
        <Skeleton className="h-40" />
      </LoadingRegion>
    )
  }
  if (error) {
    return (
      <ErrorState
        title={t('webSetCapabilities.skillsLoadFailed')}
        message={error}
        onRetry={onRetry}
      />
    )
  }

  const stats = skillStats(skills)
  const groups = groupSkills(filterSkills(skills, text.trim(), status))
  // The gap between "ready" and "offered": installed, runnable, and still never shown to the agent.
  const withheld = skills.filter(
    (s) => skillBucket(s) === 'ready' && skillAvailabilityTone(s) === 'not-offered',
  ).length
  const filters: { key: StatusFilter; label: string; count: number }[] = [
    { key: 'all', label: t('webSetCapabilities.skillsFilterAll'), count: stats.total },
    { key: 'ready', label: t('webSetCapabilities.skillsFilterReady'), count: stats.ready },
    { key: 'needs-setup', label: t('webSetCapabilities.skillsFilterNeeds'), count: stats.needs },
    { key: 'disabled', label: t('webSetCapabilities.skillsFilterDisabled'), count: stats.disabled },
  ]

  return (
    <div className="flex flex-col gap-3">
      <label className="relative block">
        <span className="sr-only">{t('webSetCapabilities.skillsFilterLabel')}</span>
        <Search
          className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
          aria-hidden
        />
        <input
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t('webSetCapabilities.skillsFilterPlaceholder')}
          className={cn(inputClass, 'pl-8')}
        />
      </label>
      <div
        role="group"
        aria-label={t('webSetCapabilities.skillsStatusFilter')}
        className="flex flex-wrap gap-1"
      >
        {filters
          .filter((f) => f.key !== 'disabled' || f.count > 0 || status === 'disabled')
          .map((f) => (
            <Button
              key={f.key}
              size="sm"
              variant={status === f.key ? 'secondary' : 'ghost'}
              aria-pressed={status === f.key}
              onClick={() => setStatus(f.key)}
            >
              {f.label}
              <span className="text-text-muted">{f.count}</span>
            </Button>
          ))}
      </div>
      {withheld > 0 ? (
        <p role="note" className="text-small text-text-muted">
          {tPlural('webSetCapabilities.skillsWithheld', withheld)}
        </p>
      ) : null}

      {groups.length === 0 ? (
        <EmptyState
          icon={<Package />}
          title={installedEmptyMessage(text.trim(), status)}
          className="py-8"
        />
      ) : (
        groups.map((g) => (
          <section key={g.key} aria-label={g.label} className="flex flex-col gap-1">
            <h2 className="flex items-baseline gap-2 font-semibold">
              {g.label}
              <span className="text-small font-normal text-text-muted">{g.skills.length}</span>
            </h2>
            <p className="text-small text-text-muted">{g.help}</p>
            <ul role="list" className="divide-y divide-border rounded-lg border border-border">
              {g.skills.map((skill) => (
                <li key={skill.name}>
                  <SkillRow skill={skill} onOpen={() => onOpen(String(skill.name))} />
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  )
}

function SkillIcon({ skill }: { skill: RawSkill }) {
  const publisher = skillPublisherId(skill)
  if (isPartner(publisher)) return <PartnerMark partner={publisher} />
  return (
    <span className="grid size-8 shrink-0 place-items-center rounded-md bg-surface-3" aria-hidden>
      {skill.emoji ? skill.emoji : <Package className="size-4 text-text-muted" />}
    </span>
  )
}

function SkillRow({ skill, onOpen }: { skill: RawSkill; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      // Named by its visible content (name, status, description): an aria-label
      // that drops the visible text fails WCAG 2.5.3, label in name.
      className="target flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-surface-2"
    >
      <SkillIcon skill={skill} />
      <span className="min-w-0 flex-1">
        {/* The spaces keep words apart in the accessible name; flex drops them visually. */}
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{skill.name}</span> <StatusChip skill={skill} />{' '}
          <AvailabilityChip skill={skill} />
        </span>{' '}
        {skill.description ? (
          <span className="block truncate text-small text-text-muted">{skill.description}</span>
        ) : null}
      </span>
    </button>
  )
}

// ── Skill details ───────────────────────────────────────────────────────────

function SkillDialog({
  skill,
  actions,
  blocked,
  reasonId,
  onClose,
  onUse,
  onRemove,
}: {
  skill: RawSkill | undefined
  actions: SkillActions
  blocked: Gate
  reasonId: string
  onClose: () => void
  onUse: (name: string) => void
  onRemove: (skill: RawSkill) => void
}) {
  return (
    <Dialog open={Boolean(skill)} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-xl">
        {skill ? (
          <SkillDetails
            skill={skill}
            actions={actions}
            blocked={blocked}
            reasonId={reasonId}
            onClose={onClose}
            onUse={onUse}
            onRemove={onRemove}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function SkillDetails({
  skill,
  actions,
  blocked,
  reasonId,
  onClose,
  onUse,
  onRemove,
}: {
  skill: RawSkill
  actions: SkillActions
  blocked: Gate
  reasonId: string
  onClose: () => void
  onUse: (name: string) => void
  onRemove: (skill: RawSkill) => void
}) {
  const name = String(skill.name)
  const needsSetup = skillStatus(skill) === 'needs_setup'
  const missingBins = needsSetup ? (skill.missing_bins ?? []) : []
  const missingEnv = needsSetup ? (skill.missing_env ?? []) : []
  const installs = (skill.missing_bins ?? []).length ? (skill.install ?? []) : []
  const requirements = skill.requirements?.items ?? []
  const canRemove = skillCanRemove(skill)
  const removeBlocked = skill.acquisition?.kind === 'hub' && !canRemove
  const homepage = safeUrl(skill.homepage)
  const describedBy = blocked.reason ? reasonId : undefined

  return (
    <>
      <DialogHeader>
        <div className="flex items-center gap-3">
          <SkillIcon skill={skill} />
          <DialogTitle>{name}</DialogTitle>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Chip title={layerHelp(skill.layer)}>{layerLabel(skill.layer)}</Chip>
          <StatusChip skill={skill} />
          <AvailabilityChip skill={skill} />
        </div>
        {skill.description ? <DialogDescription>{skill.description}</DialogDescription> : null}
      </DialogHeader>

      {skillAvailabilityTone(skill) === 'not-offered' ? (
        <section aria-label={t('webSetCapabilities.skillsAvailability')}>
          <h3 className="font-medium">{t('webSetCapabilities.skillsAvailability')}</h3>
          <p className="text-text-muted">{skillAvailabilityTitle(skill)}</p>
        </section>
      ) : null}

      {requirements.length ? (
        <section aria-label={t('webSetCapabilities.skillsRequirements')}>
          <h3 className="font-medium">{t('webSetCapabilities.skillsRequirements')}</h3>
          <ul role="list" className="flex flex-col gap-1 text-small">
            {requirements.map((item, i) => {
              const missing = [...(item.missing_bins ?? []), ...(item.missing_env ?? [])]
              return (
                <li key={item.name || i} className="flex flex-wrap items-center gap-2">
                  <span className="font-mono">{item.name}</span>
                  <Chip className={item.status === 'ready' ? 'text-ok' : 'text-text-muted'}>
                    {item.status === 'ready'
                      ? t('webSetCapabilities.skillsReqReady')
                      : item.status === 'needs_setup' || item.status === 'missing_skill'
                        ? t('webSetCapabilities.skillsReqMissing')
                        : t('webSetCapabilities.skillsReqUndeclared')}
                  </Chip>
                  {missing.length ? (
                    <span className="font-mono text-text-muted">{missing.join(', ')}</span>
                  ) : null}
                </li>
              )
            })}
          </ul>
        </section>
      ) : null}

      {missingBins.length || missingEnv.length ? (
        <section aria-label={t('webSetCapabilities.skillsMissing')} className="flex flex-col gap-2">
          <h3 className="font-medium">{t('webSetCapabilities.skillsMissing')}</h3>
          <ul role="list" className="flex flex-col gap-2">
            {missingBins.map((bin) => (
              <li key={`bin:${bin}`} className="text-small">
                <code>{bin}</code>{' '}
                <span className="text-text-muted">
                  {t('webSetCapabilities.skillsMissingBinary')}
                </span>
              </li>
            ))}
            {missingEnv.map((env) => (
              <li key={`env:${env}`}>
                <MissingEnvRow
                  name={env}
                  detail={(skill.missing_env_detail ?? []).find((d) => d.name === env)}
                  actions={actions}
                  blocked={blocked}
                  describedBy={describedBy}
                />
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {installs.length ? (
        <section aria-label={t('webSetCapabilities.skillsDeps')} className="flex flex-col gap-2">
          <h3 className="font-medium">{t('webSetCapabilities.skillsDeps')}</h3>
          {installs.map((option) => {
            const id = String(option.id)
            const busy = actions.busy.has(`deps:${name}:${id}`)
            return (
              <div key={id} className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 flex-1 text-small">
                  {option.label ||
                    t('webSetCapabilities.skillsInstallVia', { kind: String(option.kind) })}
                  {option.bins?.length ? (
                    <span className="text-text-muted"> ({option.bins.join(', ')})</span>
                  ) : null}
                </span>
                <Button
                  size="sm"
                  disabled={blocked.disabled || busy}
                  aria-describedby={describedBy}
                  onClick={async () => {
                    const left = await actions.installDeps(name, id)
                    if (left === 0) onClose()
                  }}
                >
                  <Download aria-hidden />
                  {busy
                    ? t('webSetCapabilities.skillsInstalling')
                    : t('webSetCapabilities.skillsInstallVia', { kind: String(option.kind) })}
                </Button>
              </div>
            )
          })}
        </section>
      ) : null}

      {homepage ? (
        <a
          href={homepage}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline"
        >
          {t('webSetCapabilities.skillsHomepage')}
        </a>
      ) : null}

      {removeBlocked ? (
        <p className="text-small text-text-muted">{t('webSetCapabilities.skillsRemoveBlocked')}</p>
      ) : skill.file_path ? (
        <p className="font-mono text-small break-all text-text-muted">{skill.file_path}</p>
      ) : null}

      <DialogFooter>
        {canRemove ? (
          <Button
            variant="danger"
            size="sm"
            disabled={blocked.disabled || actions.busy.has(`uninstall:${name}`)}
            aria-describedby={describedBy}
            onClick={() => onRemove(skill)}
          >
            <Trash2 aria-hidden />
            {t('webSetCapabilities.skillsRemove')}
          </Button>
        ) : null}
        {skillCanUpdate(skill) ? (
          <Button
            variant="outline"
            size="sm"
            disabled={blocked.disabled || actions.busy.has(`update:${name}`)}
            aria-describedby={describedBy}
            onClick={() => void actions.update(name)}
          >
            <RefreshCw aria-hidden />
            {actions.busy.has(`update:${name}`)
              ? t('webSetCapabilities.skillsUpdating')
              : t('webSetCapabilities.skillsUpdate')}
          </Button>
        ) : null}
        <Button size="sm" variant="primary" onClick={() => onUse(name)}>
          <MessageSquare aria-hidden />
          {t('webSetCapabilities.skillsUse')}
        </Button>
      </DialogFooter>
    </>
  )
}

/** A missing variable with an inline, masked "Set" form (`env.set`). */
function MissingEnvRow({
  name,
  detail,
  actions,
  blocked,
  describedBy,
}: {
  name: string
  detail?: { description?: string; url?: string }
  actions: SkillActions
  blocked: Gate
  describedBy?: string
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [shown, toggleShown] = useReveal()
  const inputId = useId()
  const saving = actions.busy.has(`env:${name}`)
  const url = safeUrl(detail?.url)

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2 text-small">
        <code>{name}</code>
        <span className="text-text-muted">{t('webSetCapabilities.skillsMissingEnv')}</span>
        <span className="flex-1" />
        {!editing ? (
          <Button
            size="sm"
            variant="outline"
            aria-label={t('webSetCapabilities.skillsSetEnvFor', { name })}
            disabled={blocked.disabled}
            aria-describedby={describedBy}
            onClick={() => setEditing(true)}
          >
            {t('webSetCapabilities.skillsSetEnv')}
          </Button>
        ) : null}
      </div>
      {detail?.description || url ? (
        <p className="text-small text-text-muted">
          {detail?.description}{' '}
          {url ? (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline"
            >
              {t('webSetCapabilities.skillsWhereToGet')}
            </a>
          ) : null}
        </p>
      ) : null}
      {editing ? (
        <form
          className="flex items-center gap-2"
          onSubmit={async (e) => {
            e.preventDefault()
            if (!value) return
            if (await actions.setEnv(name, value)) {
              setValue('')
              setEditing(false)
            }
          }}
        >
          <label htmlFor={inputId} className="sr-only">
            {t('webSetCapabilities.skillsEnvValue', { name })}
          </label>
          <input
            id={inputId}
            type={shown ? 'text' : 'password'}
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className={inputClass}
            autoFocus
          />
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={shown ? t('webSetCapabilities.hide') : t('webSetCapabilities.reveal')}
            onClick={toggleShown}
          >
            {shown ? <EyeOff /> : <Eye />}
          </Button>
          <Button
            type="submit"
            size="sm"
            variant="primary"
            disabled={blocked.disabled || saving || !value}
            aria-describedby={describedBy}
          >
            {saving ? t('webSetCapabilities.saving') : t('webSetCapabilities.save')}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            {t('webCommon.cancel')}
          </Button>
        </form>
      ) : null}
    </div>
  )
}

// ── Catalogs ────────────────────────────────────────────────────────────────

function useDebounced(value: string, delay: number): string {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(id)
  }, [value, delay])
  return debounced
}

function CatalogPanel({
  source,
  actions,
  blocked,
  reasonId,
}: {
  source: CatalogSource
  actions: SkillActions
  blocked: Gate
  reasonId: string
}) {
  const [text, setText] = useState('')
  const [category, setCategory] = useState('all')
  const community = source === 'community'
  // Partner catalogs are small and filtered here; Community asks the server,
  // which also matches tags the payload never carries.
  const query = useDebounced(community ? text.trim() : '', REGISTRY_SEARCH_DEBOUNCE_MS)
  const snapshot = useCatalog(source, '', true)
  const live = useCatalog(source, query, community && query.length > 0)
  const rows = (query ? live.data : undefined) ?? snapshot.data ?? []
  const serverFiltered = Boolean(query && live.data) && text.trim() === query
  const items = filterRegistry(rows, category, text, { serverFiltered })
  const chips = categoryChips(snapshot.data ?? [], category)
  const failed = query ? live.error : snapshot.error
  const loading = query ? live.isPending : snapshot.isPending
  const label = community ? t('webSetCapabilities.skillsTabCommunity') : partnerCopy(source).name

  return (
    <div className="flex flex-col gap-3">
      {community ? (
        <>
          <div className="flex items-start gap-3">
            <Globe className="mt-0.5 size-6 shrink-0 text-text-muted" aria-hidden />
            <div>
              <h2 className="font-semibold">{t('webSetCapabilities.skillsCommunityTitle')}</h2>
              <p className="text-text-muted">{t('webSetCapabilities.skillsCommunityBody')}</p>
            </div>
          </div>
          <GithubInstall actions={actions} blocked={blocked} reasonId={reasonId} />
        </>
      ) : (
        <PartnerIntro partner={source} count={snapshot.data?.length} />
      )}

      <label className="relative block">
        <span className="sr-only">{t('webSetCapabilities.skillsSearchLabel', { label })}</span>
        <Search
          className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-text-muted"
          aria-hidden
        />
        <input
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t('webSetCapabilities.skillsSearchPlaceholder', { label })}
          className={cn(inputClass, 'pl-8')}
        />
      </label>

      {chips.length ? (
        <div
          role="group"
          aria-label={t('webSetCapabilities.skillsCategories')}
          className="flex flex-wrap gap-1"
        >
          {chips.map((c) => (
            <Button
              key={c.cat}
              size="sm"
              variant={c.active ? 'secondary' : 'ghost'}
              aria-pressed={c.active}
              onClick={() => setCategory(c.cat)}
            >
              {c.label}
              <span className="text-text-muted">{c.count}</span>
            </Button>
          ))}
        </div>
      ) : null}

      {failed ? (
        <ErrorState
          title={t('webSetCapabilities.skillsCatalogFailed')}
          message={errorMessage(failed)}
          onRetry={() => void (query ? live.refetch() : snapshot.refetch())}
        />
      ) : loading ? (
        <LoadingRegion>
          <Skeleton className="h-40" />
        </LoadingRegion>
      ) : items.length === 0 ? (
        <EmptyState title={registryEmptyMessage(source, text)} className="py-8" />
      ) : (
        <ul
          role="list"
          aria-label={label}
          className="divide-y divide-border rounded-lg border border-border"
        >
          {items.map((item) => (
            <li key={registryKey(item)}>
              <CatalogRow item={item} actions={actions} blocked={blocked} reasonId={reasonId} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function PartnerIntro({ partner, count }: { partner: Partner; count?: number }) {
  const copy = partnerCopy(partner)
  return (
    <div className="flex items-start gap-3 rounded-lg border border-border bg-surface-1 p-3">
      <PartnerMark partner={partner} className="size-10" />
      <div className="min-w-0 flex-1">
        <h2 className="font-semibold">{copy.title}</h2>
        <p className="text-text-muted">{copy.description}</p>
        <p role="note" className="mt-1 flex items-start gap-1.5 text-small">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
          {copy.notice}
        </p>
      </div>
      {typeof count === 'number' ? (
        <span className="text-small whitespace-nowrap text-text-muted">
          {tPlural('webSetCapabilities.skillsCount', count)}
        </span>
      ) : null}
    </div>
  )
}

function CatalogRow({
  item,
  actions,
  blocked,
  reasonId,
}: {
  item: RegistryItem
  actions: SkillActions
  blocked: Gate
  reasonId: string
}) {
  const key = registryKey(item)
  const action = installAction(item, new Set(actions.armed))
  const busy = actions.busy.has(key)
  const homepage = safeUrl(item.homepage)
  const source = String(item.source || '').toLowerCase()
  const category = item.category && item.category !== 'other' ? item.category : ''

  return (
    <div className="flex items-start gap-3 px-3 py-2">
      {isPartner(source) ? (
        <PartnerMark partner={source} />
      ) : (
        <span
          className="grid size-8 shrink-0 place-items-center rounded-md bg-surface-3"
          aria-hidden
        >
          {item.emoji ? item.emoji : <Package className="size-4 text-text-muted" />}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{item.name}</span>
          {item.provider ? (
            <span className="text-small text-text-muted">{item.provider}</span>
          ) : null}
          {category ? <Chip>{catLabel(category)}</Chip> : null}
        </div>
        {item.description ? <p className="text-small text-text-muted">{item.description}</p> : null}
        <p className="flex flex-wrap gap-2 font-mono text-small text-text-muted">
          <span>{item.source}</span>
          {homepage ? (
            <a
              href={homepage}
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline"
            >
              {t('webSetCapabilities.skillsSource')}
            </a>
          ) : null}
        </p>
      </div>
      {action === 'installed' ? (
        <Chip className="border-ok/40 text-ok">
          <Check className="size-3.5" aria-hidden />
          {t('webSetCapabilities.skillsInstalledBadge')}
        </Chip>
      ) : (
        <Button
          size="sm"
          variant={action === 'force' ? 'danger' : 'primary'}
          disabled={blocked.disabled || busy}
          aria-describedby={blocked.reason ? reasonId : undefined}
          aria-label={
            action === 'force'
              ? t('webSetCapabilities.skillsForceInstallNamed', { name: String(item.name) })
              : t('webSetCapabilities.skillsInstallNamed', { name: String(item.name) })
          }
          onClick={() => void actions.install(key, installSource(item), action === 'force')}
        >
          {action === 'force' ? <TriangleAlert aria-hidden /> : <Download aria-hidden />}
          {busy
            ? t('webSetCapabilities.skillsInstalling')
            : action === 'force'
              ? t('webSetCapabilities.skillsForceInstall')
              : t('webSetCapabilities.skillsInstall')}
        </Button>
      )}
    </div>
  )
}

function GithubInstall({
  actions,
  blocked,
  reasonId,
}: {
  actions: SkillActions
  blocked: Gate
  reasonId: string
}) {
  const [url, setUrl] = useState('')
  const inputId = useId()
  const target = url.trim()
  const busy = actions.busy.has(target)
  return (
    <form
      className="flex flex-col gap-1 rounded-lg border border-border p-3"
      onSubmit={async (e) => {
        e.preventDefault()
        if (!target || blocked.disabled) return
        if (await actions.install(target, 'github', false)) setUrl('')
      }}
    >
      <label htmlFor={inputId} className="font-medium">
        {t('webSetCapabilities.skillsGithubLabel')}
      </label>
      <p className="text-small text-text-muted">{t('webSetCapabilities.skillsGithubHelp')}</p>
      <div className="flex gap-2">
        <input
          id={inputId}
          type="url"
          autoComplete="off"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={t('webSetCapabilities.skillsGithubPlaceholder')}
          className={inputClass}
        />
        <Button
          type="submit"
          variant="primary"
          disabled={blocked.disabled || !target || busy}
          aria-describedby={blocked.reason ? reasonId : undefined}
        >
          <Download aria-hidden />
          {busy ? t('webSetCapabilities.skillsInstalling') : t('webSetCapabilities.skillsInstall')}
        </Button>
      </div>
    </form>
  )
}
