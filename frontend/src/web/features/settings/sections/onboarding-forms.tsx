import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CircleAlert, Copy, Loader2, Terminal } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import '@/i18n/en/webSettings'
import { copyWithFallback } from '@/lib/clipboard'
import { gate, requireConnection } from '@/lib/disabled-reason'
import { cn } from '@/lib/utils'
import {
  loadSettingsSnapshot,
  SETTINGS_SNAPSHOT_QUERY_KEY,
  type SettingsSnapshot,
} from '@/views/settings/snapshot'
import {
  buildRouterConfigureParams,
  envRecoveryCommand,
  isVisibleTier,
  resolveJudgeModelParam,
  routerMode,
  TEXT_TIERS,
  type FieldSpec,
  type ProviderSpec,
  type RouterMode,
  type TierSpec,
} from '@/views/setup/logic'
import { Button } from '@/web/components/ui/button'
import { Input, Select } from '@/web/components/ui/input'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Switch } from '@/web/components/ui/switch'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { afterConfigWrite } from '../config-editor'
import { SecretInput, useReveal } from '../fields'
import { WriteBlockedNotice } from '../SaveBar'
import { Disclosure, Notice, SettingsCard } from '../ui'
import {
  collectParams,
  isSecretField,
  visibleFields,
  type CapabilityAdapter,
  type FormValues,
} from './onboarding-adapters'

// Forms driven by the onboarding catalog (Models & routing, Search, Media and
// the guided setup share them). Each submits one onboarding.*.configure call
// with the revision the snapshot was read at. Those calls rewrite a whole
// section, so every form starts from the saved values and sends all of them.

export function useSetupSnapshot() {
  const rpc = useRpc()
  return useQuery({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
  })
}

interface ConfigureResult {
  restartRequired?: boolean
  warnings?: string[]
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Run one configure call and refresh what depends on it. */
export function useConfigure() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** `onApplied` runs before the refetch, so the form is clean when new values land. */
  const run = async (
    method: string,
    params: Record<string, unknown>,
    revision: string | null | undefined,
    onApplied?: () => void,
  ) => {
    setBusy(true)
    setError(null)
    try {
      const result = await rpc.call<ConfigureResult>(method, {
        ...params,
        ...(revision ? { expectedRevision: revision } : {}),
      })
      onApplied?.()
      toast.success(t('webSetConfig.applied'))
      for (const warning of result?.warnings ?? []) toast.warning(warning)
      await afterConfigWrite(queryClient, result)
      return true
    } catch (e) {
      setError(t('webSetConfig.applyFailed', { message: errorMessage(e) }))
      return false
    } finally {
      setBusy(false)
    }
  }
  return { run, busy, error }
}

export function SnapshotGate({
  children,
}: {
  children: (snapshot: SettingsSnapshot) => React.ReactNode
}) {
  const snapshot = useSetupSnapshot()
  if (snapshot.isPending) {
    return (
      <LoadingRegion className="flex flex-col gap-4">
        {[0, 1].map((i) => (
          <div key={i} className="set-card flex flex-col gap-4">
            <Skeleton className="h-5 w-40" />
            <div className="flex flex-col gap-3">
              <Skeleton className="h-9" />
              <Skeleton className="h-9" />
              <Skeleton className="h-9 w-2/3" />
            </div>
            <Skeleton className="h-8 w-20 self-end" />
          </div>
        ))}
      </LoadingRegion>
    )
  }
  if (snapshot.isError) {
    return (
      <ErrorState
        title={t('webSetConfig.loadFailed')}
        message={errorMessage(snapshot.error)}
        onRetry={() => void snapshot.refetch()}
      />
    )
  }
  return <>{children(snapshot.data)}</>
}

/**
 * The catalog's "what you need" items as running text. Each item is a
 * sentence of its own; one without closing punctuation gets a full stop.
 */
export function needsSentence(items: readonly string[]): string {
  return items
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => (/[.!?)]$/.test(item) ? item : `${item}.`))
    .join(' ')
}

/**
 * A form draft seeded from the saved configuration. While it is untouched it
 * follows the snapshot (a provider switch elsewhere reseeds it); once edited
 * it keeps the edits and reports `stale` when the saved values move under it.
 */
function useSeededDraft<T>(seed: T, key: string) {
  const [state, setState] = useState({ key, value: seed, dirty: false })
  let current = state
  if (state.key !== key && !state.dirty) {
    current = { key, value: seed, dirty: false }
    setState(current)
  }
  return {
    value: current.value,
    dirty: current.dirty,
    stale: current.key !== key,
    update: (patch: (value: T) => T) =>
      setState((s) => ({ ...s, value: patch(s.value), dirty: true })),
    /** Start over from `value` (a provider switch) as an edit or as clean. */
    replace: (value: T, nextKey: string, dirty: boolean) =>
      setState({ key: nextKey, value, dirty }),
    reload: () => setState({ key, value: seed, dirty: false }),
    /** After a successful Apply: clean, and anything `scrub` drops (secrets) gone. */
    applied: (scrub: (value: T) => T) =>
      setState((s) => ({ ...s, value: scrub(s.value), dirty: false })),
  }
}

/** The shared footer: Apply, connection / write-blocked reasons, the last error. */
function ApplyBar({
  snapshot,
  busy,
  error,
  disabled,
  onApply,
}: {
  snapshot: SettingsSnapshot
  busy: boolean
  error: string | null
  disabled?: boolean
  onApply(): void
}) {
  const connection = useDisplayedConnection()
  const blocked = gate(requireConnection(connection))
  return (
    <div className="mt-4 flex flex-wrap items-center justify-end gap-x-3 gap-y-2 border-t border-border pt-3.5">
      {blocked.reason ? (
        <span className="mr-auto text-small text-text-muted">{blocked.reason}</span>
      ) : null}
      {error ? (
        <span
          role="alert"
          className="anim-fade-in mr-auto flex min-w-0 items-center gap-1.5 text-small text-danger"
        >
          <CircleAlert className="size-3.5 shrink-0" aria-hidden data-status-icon />
          {error}
        </span>
      ) : null}
      <Button
        variant="primary"
        size="sm"
        className="min-w-20"
        disabled={busy || blocked.disabled || Boolean(snapshot.writeBlocked) || disabled}
        onClick={onApply}
      >
        {busy ? <Loader2 className="animate-spin" /> : <Check />}
        {busy ? t('webSetConfig.applying') : t('webSetConfig.apply')}
      </Button>
    </div>
  )
}

/** Above a form: the write-blocked state, or saved values that moved under an edit. */
function FormNotices({
  snapshot,
  stale,
  onReload,
}: {
  snapshot: SettingsSnapshot
  stale: boolean
  onReload(): void
}) {
  const queryClient = useQueryClient()
  return (
    <>
      {snapshot.writeBlocked ? (
        <WriteBlockedNotice
          onReload={() =>
            void queryClient.invalidateQueries({ queryKey: SETTINGS_SNAPSHOT_QUERY_KEY })
          }
        />
      ) : null}
      {stale ? (
        <Notice
          role="status"
          tone="warn"
          className="anim-enter mb-3"
          actions={
            <Button size="sm" variant="outline" onClick={onReload}>
              {t('webSettings.reload')}
            </Button>
          }
        >
          {t('webSetConfig.changedElsewhere')}
        </Notice>
      ) : null}
    </>
  )
}

/**
 * One catalog/router setting in the shared settings-row anatomy (fields.tsx
 * FieldRow): label and help on the left, the control on the right on wide
 * panes. Kept local because the label may carry a "(Required)" suffix.
 */
function SpecRow({
  fieldId,
  inputId,
  label,
  help,
  helpId,
  children,
}: {
  fieldId: string
  inputId: string
  label: React.ReactNode
  help?: React.ReactNode
  helpId?: string
  children: React.ReactNode
}) {
  return (
    <div data-field-id={fieldId} className="set-row">
      <label htmlFor={inputId} className="set-row-label">
        {label}
      </label>
      {help ? (
        <p id={helpId} className="set-row-help">
          {help}
        </p>
      ) : null}
      <div className="set-row-control">{children}</div>
    </div>
  )
}

function SpecField({
  field,
  fieldId,
  value,
  onChange,
}: {
  field: FieldSpec
  fieldId: string
  value: unknown
  onChange(value: unknown): void
}) {
  const id = useId()
  const helpId = `${id}-help`
  const [shown, toggle] = useReveal()
  const label = field.label || field.name
  const secret = isSecretField(field)
  const help = field.description || field.help
  const describedBy = help ? helpId : undefined
  let control: React.ReactNode
  if (field.type === 'bool') {
    control = (
      <Switch
        id={id}
        aria-describedby={describedBy}
        checked={Boolean(value)}
        onCheckedChange={(v) => onChange(v)}
      />
    )
  } else if (field.type === 'select' && field.choices) {
    control = (
      <Select
        id={id}
        aria-describedby={describedBy}
        value={String(value ?? '')}
        onChange={(e) => onChange(e.target.value)}
      >
        {field.choices.map((c) => (
          <option key={c} value={c}>
            {c === '' ? t('webSettings.optionDefault') : c}
          </option>
        ))}
      </Select>
    )
  } else {
    const text = value === undefined || value === null ? '' : String(value)
    control = secret ? (
      <SecretInput
        id={id}
        aria-describedby={describedBy}
        shown={shown}
        onToggle={toggle}
        placeholder={t('webSetConfig.keepSecret')}
        value={text}
        onChange={(e) => onChange(e.target.value)}
      />
    ) : (
      <Input
        id={id}
        aria-describedby={describedBy}
        type={field.type === 'int' || field.type === 'float' ? 'number' : 'text'}
        autoComplete="off"
        placeholder={field.placeholder}
        value={text}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
  return (
    <SpecRow
      fieldId={fieldId}
      inputId={id}
      helpId={helpId}
      help={help}
      label={
        <>
          {label}
          {field.required && !secret ? (
            <span className="ml-1 text-small font-normal text-text-muted">
              ({t('webSetConfig.required')})
            </span>
          ) : null}
        </>
      }
    >
      {control}
    </SpecRow>
  )
}

/** A copyable CLI command (env recovery for a capability). */
export function CommandLine({
  command,
  onCopy,
  className,
}: {
  command: string
  /** Replaces the default copy (clipboard with fallback, then a toast). */
  onCopy?(command: string): void
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex min-w-0 items-center gap-2 rounded-control border border-border bg-surface-inset pl-3 shadow-inset',
        className,
      )}
    >
      <Terminal className="size-3.5 shrink-0 text-text-muted" aria-hidden />
      <code
        tabIndex={0}
        className="min-w-0 flex-1 overflow-x-auto py-2 font-mono text-small whitespace-pre"
      >
        {command}
      </code>
      <Button
        size="icon-sm"
        variant="ghost"
        className="mr-1"
        aria-label={t('webSetConfig.copyCommand')}
        onClick={() =>
          onCopy
            ? onCopy(command)
            : void copyWithFallback(command)
                .then(() => toast.success(t('webSetConfig.copied')))
                .catch(() => undefined)
        }
      >
        <Copy />
      </Button>
    </div>
  )
}

interface SpecDraft {
  providerId: string
  values: FormValues
}

/**
 * Choose one spec from a catalog list and fill its fields, starting from what
 * is saved. Secrets start blank: left blank they keep the stored value (the
 * gateway's rule); typed, they are sent once and cleared after Apply.
 */
export function SpecForm({
  title,
  adapter,
  snapshot,
  status,
  recovery,
  aside,
  children,
  buildParams,
}: {
  title: string
  adapter: CapabilityAdapter
  snapshot: SettingsSnapshot
  /** A one-line status for the capability (ready, missing key, …). */
  status?: string
  /** The `envRecoveryCommands` section whose command applies here. */
  recovery?: string
  /** Extra information about the chosen spec (e.g. the router-support badge). */
  aside?(spec: ProviderSpec | undefined): React.ReactNode
  children?: React.ReactNode
  buildParams?(providerId: string, values: FormValues, fields: FieldSpec[]): Record<string, unknown>
}) {
  const specs = adapter.specs(snapshot)
  const current = adapter.current(snapshot)
  const initial = specs.some((s) => s.providerId === current)
    ? current
    : (specs[0]?.providerId ?? '')
  const seedFor = (providerId: string): SpecDraft => {
    const spec = specs.find((s) => s.providerId === providerId)
    return { providerId, values: spec ? adapter.seed(snapshot, spec) : {} }
  }
  // Keyed on what the seed is, so a refetch that changes nothing keeps the draft.
  const seed = seedFor(initial)
  const draft = useSeededDraft<SpecDraft>(seed, JSON.stringify(seed))
  const { providerId, values } = draft.value
  const spec = specs.find((s) => s.providerId === providerId)
  const fields = visibleFields(spec ? adapter.fields(spec) : [], values)
  const configure = useConfigure()
  const selectId = useId()
  const [params] = useSearchParams()
  const focusField = params.get('field') ?? ''
  const advanced = fields.filter((f) => spec && adapter.advanced?.(f, spec))
  const core = fields.filter((f) => !advanced.includes(f))
  const advancedOpen = advanced.some(
    (f) => f.required || adapter.fieldPath(f.name, providerId) === focusField,
  )

  const switchProvider = (next: string) => {
    const fresh = seedFor(next)
    // Section-wide settings (e.g. search's result count) are not the provider's.
    for (const name of adapter.shared ?? []) {
      if (name in values && name in fresh.values) fresh.values[name] = values[name]
    }
    // A switch is an edit: a refetch must not snap the form back to the saved provider.
    draft.replace(fresh, JSON.stringify(seed), true)
  }

  const setValue = (name: string, value: unknown) =>
    draft.update((d) => ({ ...d, values: { ...d.values, [name]: value } }))

  const submit = () => {
    if (!spec) return
    const params = buildParams
      ? buildParams(providerId, values, fields)
      : collectParams(providerId, fields, values)
    void configure.run(adapter.method, params, snapshot.revision, () =>
      draft.applied((d) => ({
        ...d,
        values: Object.fromEntries(
          Object.entries(d.values).map(([name, v]) => {
            const field = fields.find((f) => f.name === name)
            return [name, field && isSecretField(field) ? '' : v]
          }),
        ),
      })),
    )
  }

  const renderField = (field: FieldSpec) => (
    <SpecField
      key={field.name}
      field={field}
      fieldId={adapter.fieldPath(field.name, providerId)}
      value={values[field.name]}
      onChange={(v) => setValue(field.name, v)}
    />
  )
  const command = recovery ? envRecoveryCommand(snapshot.status ?? {}, recovery) : ''

  return (
    <SettingsCard aria-label={title} title={title} description={status}>
      {command ? <CommandLine command={command} className="mt-3" /> : null}
      <div className="mt-3 empty:hidden">
        <FormNotices snapshot={snapshot} stale={draft.stale} onReload={draft.reload} />
        {children}
      </div>
      {!adapter.single ? (
        <SpecRow
          fieldId={adapter.providerPath}
          inputId={selectId}
          label={t('webSetConfig.provider')}
        >
          <Select id={selectId} value={providerId} onChange={(e) => switchProvider(e.target.value)}>
            {specs.map((s) => (
              <option key={s.providerId} value={s.providerId}>
                {s.label || s.providerId}
              </option>
            ))}
          </Select>
        </SpecRow>
      ) : null}
      {aside || spec?.whatYouNeed?.length ? (
        <div className="mb-1 flex flex-col gap-1 rounded-control bg-surface-2 px-3 py-2 text-small empty:hidden">
          {aside?.(spec)}
          {spec?.whatYouNeed?.length ? (
            <p className="text-text-muted">
              {t('webSetConfig.whatYouNeed', { items: needsSentence(spec.whatYouNeed) })}
            </p>
          ) : null}
        </div>
      ) : null}
      {core.map(renderField)}
      {advanced.length ? (
        <Disclosure
          className="mt-2 border-t border-border pt-2"
          open={advancedOpen || undefined}
          summary={t('webSetConfig.advancedFields')}
          bodyClassName="pt-0"
        >
          {advanced.map(renderField)}
        </Disclosure>
      ) : null}
      <ApplyBar
        snapshot={snapshot}
        busy={configure.busy}
        error={configure.error}
        disabled={!spec}
        onApply={submit}
      />
    </SettingsCard>
  )
}

// ── Pilot Router ───────────────────────────────────────────────────────────

// Thinking levels the router accepts; '' leaves the tier on its default.
const THINKING_LEVELS = ['', 'off', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh'] as const

interface TierRow {
  model: string
  thinkingLevel: string
  supportsImage: boolean
  /** Fields the form does not show (`description`, …), sent back as they are. */
  extra: Record<string, unknown>
}

interface RouterDraft {
  /** The provider the tier rows were seeded for. */
  provider: string
  mode: RouterMode
  defaultTier: string
  judge: string
  pilotThreshold: string
  jevApiKey: string
  jevHighRisk: string
  /** 'off', or the tier translation is capped at. */
  translate: string
  rows: Record<string, TierRow>
}

const SHOWN_TIER_KEYS = new Set([
  'provider',
  'model',
  'thinking_level',
  'thinkingLevel',
  'supports_image',
  'supportsImage',
])

function tierRow(tier: TierSpec | undefined): TierRow {
  const extra = Object.fromEntries(
    Object.entries(tier ?? {}).filter(([key, value]) => !SHOWN_TIER_KEYS.has(key) && value != null),
  )
  return {
    model: String(tier?.model ?? ''),
    thinkingLevel: String(tier?.thinkingLevel ?? tier?.thinking_level ?? ''),
    supportsImage: Boolean(tier?.supportsImage ?? tier?.supports_image),
    extra,
  }
}

/**
 * The tier table for `provider`: a saved tier where it belongs to that
 * provider, else the provider's profile default. A saved tier of another
 * provider (left behind by a provider switch while the router was off, or the
 * shipped OpenRouter defaults) is that provider's model id: it is never
 * offered, or sent, under this one.
 */
function providerTiers(
  provider: string,
  profileTiers: Record<string, TierSpec>,
  savedTiers: Record<string, TierSpec>,
): Record<string, TierSpec> {
  const own = (tier: TierSpec | undefined) => {
    const owner = String(tier?.provider ?? '')
      .trim()
      .toLowerCase()
    return !owner || owner === provider.trim().toLowerCase()
  }
  const out: Record<string, TierSpec> = {}
  for (const name of new Set([...Object.keys(profileTiers), ...Object.keys(savedTiers)])) {
    const saved = savedTiers[name]
    if (saved && own(saved)) out[name] = saved
    else if (profileTiers[name]) out[name] = profileTiers[name]
  }
  return out
}

/** The tiers the form edits: the text tiers, then `image_model`. */
function visibleTierNames(tiers: Record<string, TierSpec>): string[] {
  const order = [...TEXT_TIERS, 'image_model'] as string[]
  return Object.keys(tiers)
    .filter(isVisibleTier)
    .sort((a, b) => order.indexOf(a) - order.indexOf(b))
}

function routerSeed(snapshot: SettingsSnapshot): RouterDraft {
  const router = snapshot.config?.agentos_router ?? {}
  const profiles = snapshot.catalog?.routerProfiles
  const provider = snapshot.config?.llm?.provider ?? ''
  const profileTiers = profiles?.profiles?.find((p) => p.providerId === provider)?.tiers ?? {}
  const merged = providerTiers(provider, profileTiers, router.tiers ?? {})
  const threshold = (value: number | null | undefined, fallback: string) =>
    value === null || value === undefined ? fallback : String(value)
  return {
    provider,
    mode: routerMode(router),
    defaultTier: router.default_tier || profiles?.defaultTier || 'c1',
    judge: router.judge_model || '',
    pilotThreshold: threshold(router.pilot?.safety_net_threshold, '0.5'),
    jevApiKey: '',
    jevHighRisk: threshold(router.jev?.high_risk_threshold, '0.7'),
    // An absent key means the default: capped, at c0.
    translate:
      router.translate_ceiling_enabled === false ? 'off' : router.translate_ceiling_tier || 'c0',
    rows: Object.fromEntries(visibleTierNames(merged).map((name) => [name, tierRow(merged[name])])),
  }
}

/** The onboarding.router.configure params for a router draft. */
export function routerParams(snapshot: SettingsSnapshot, draft: RouterDraft) {
  const router = snapshot.config?.agentos_router ?? {}
  const provider = snapshot.config?.llm?.provider ?? ''
  return buildRouterConfigureParams({
    sel: draft.mode,
    defaultTier: draft.defaultTier,
    judgeModel: resolveJudgeModelParam(
      draft.judge,
      router.judge_model || '',
      Boolean(router.judge_base_url),
    ),
    pilotThresholdRaw: draft.pilotThreshold,
    jevApiKeyRaw: draft.jevApiKey,
    jevHighRiskThresholdRaw: draft.jevHighRisk,
    translateCeilingEnabled: draft.translate !== 'off',
    translateCeilingTier:
      draft.translate === 'off' ? router.translate_ceiling_tier || 'c0' : draft.translate,
    // Every visible tier, image_model included: in recommended mode the
    // gateway rebuilds the tiers from the profile plus what is sent. Requests
    // always go through llm.provider, so that is each tier's provider.
    tiers: Object.entries(draft.rows).map(([tier, row]) => ({ tier, provider, ...row })),
  })
}

/** Pilot Router mode, default tier, per-mode settings and the tier table. */
export function RouterForm({ snapshot }: { snapshot: SettingsSnapshot }) {
  const router = snapshot.config?.agentos_router ?? {}
  const provider = snapshot.config?.llm?.provider ?? ''
  const seed = useMemo(() => routerSeed(snapshot), [snapshot])
  const draft = useSeededDraft(seed, `${provider}|${JSON.stringify(seed)}`)
  // An edited draft keeps its edits when the saved values move, but its tier
  // rows belong to the provider they were seeded for: after a provider switch
  // they are the new provider's, whatever was typed before it.
  const forProvider = (v: RouterDraft): RouterDraft =>
    v.provider === provider ? v : { ...v, provider, rows: seed.rows }
  const d = forProvider(draft.value)
  const configure = useConfigure()
  const id = useId()
  const set = (patch: Partial<RouterDraft>) =>
    draft.update((v) => ({ ...forProvider(v), ...patch }))
  const setRow = (tier: string, patch: Partial<TierRow>) =>
    draft.update((v) => {
      const cur = forProvider(v)
      return { ...cur, rows: { ...cur.rows, [tier]: { ...cur.rows[tier]!, ...patch } } }
    })

  const judgeProfile = snapshot.catalog?.routerProfiles?.judge?.profiles?.[provider] ?? {}
  const judgeModels = (judgeProfile.models ?? []).filter((m) => typeof m === 'string')
  const judgeAuto = judgeProfile.autoModel
  // A saved judge the catalog does not list stays selectable.
  if (router.judge_model && !judgeModels.includes(router.judge_model)) {
    judgeModels.unshift(router.judge_model)
  }

  const submit = () =>
    void configure.run(
      'onboarding.router.configure',
      routerParams(snapshot, d) as unknown as Record<string, unknown>,
      snapshot.revision,
      () => draft.applied((v) => ({ ...v, jevApiKey: '' })),
    )

  const select = (
    fieldId: string,
    label: string,
    value: string,
    options: readonly { value: string; label: string }[],
    onChange: (value: string) => void,
    hint?: string,
  ) => (
    <SpecRow
      fieldId={fieldId}
      inputId={`${id}-${fieldId}`}
      label={label}
      help={hint}
      helpId={`${id}-${fieldId}-hint`}
    >
      <Select
        id={`${id}-${fieldId}`}
        value={value}
        aria-describedby={hint ? `${id}-${fieldId}-hint` : undefined}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </Select>
    </SpecRow>
  )
  const numberField = (
    fieldId: string,
    label: string,
    value: string,
    hint: string,
    key: keyof RouterDraft,
  ) => (
    <SpecRow
      fieldId={fieldId}
      inputId={`${id}-${fieldId}`}
      label={label}
      help={hint}
      helpId={`${id}-${fieldId}-hint`}
    >
      <Input
        id={`${id}-${fieldId}`}
        type="number"
        min={0}
        max={1}
        step={0.05}
        className="tabular-nums"
        value={value}
        aria-describedby={`${id}-${fieldId}-hint`}
        onChange={(e) => set({ [key]: e.target.value } as Partial<RouterDraft>)}
      />
    </SpecRow>
  )
  const tierOptions = TEXT_TIERS.map((tier) => ({ value: tier, label: tier }))

  return (
    <SettingsCard
      aria-label={t('webSetConfig.router')}
      title={t('webSetConfig.router')}
      description={t('webSetConfig.routerHelp')}
    >
      <div className="mt-3 empty:hidden">
        <FormNotices snapshot={snapshot} stale={draft.stale} onReload={draft.reload} />
        {!provider ? (
          <Notice role="status" tone="info" className="mb-1">
            {t('webSetConfig.routerNeedsProvider')}
          </Notice>
        ) : null}
      </div>
      {select(
        'agentos_router.strategy',
        t('webSetConfig.routerMode'),
        d.mode,
        [
          { value: 'pilot-v1', label: t('webSetConfig.routerModePilot') },
          { value: 'llm_judge', label: t('webSetConfig.routerModeJudge') },
          { value: 'jev', label: t('webSetConfig.routerModeJev') },
          { value: 'disabled', label: t('webSetConfig.routerModeDisabled') },
        ],
        (v) => set({ mode: v as RouterMode }),
        d.mode === 'jev' ? t('webSetConfig.routerJevHint') : undefined,
      )}
      {select(
        'agentos_router.default_tier',
        t('webSetConfig.defaultTier'),
        d.defaultTier,
        tierOptions,
        (v) => set({ defaultTier: v }),
      )}
      {d.mode === 'llm_judge'
        ? select(
            'agentos_router.judge_model',
            t('webSetConfig.judgeModel'),
            d.judge,
            [
              {
                value: '',
                label: judgeAuto
                  ? t('webSetConfig.judgeAutoModel', { model: judgeAuto })
                  : t('webSetConfig.judgeAuto'),
              },
              ...judgeModels.map((m) => ({ value: m, label: m })),
            ],
            (v) => set({ judge: v }),
          )
        : null}
      {d.mode === 'pilot-v1'
        ? numberField(
            'agentos_router.pilot.safety_net_threshold',
            t('webSetConfig.pilotThreshold'),
            d.pilotThreshold,
            t('webSetConfig.pilotThresholdHint'),
            'pilotThreshold',
          )
        : null}
      {d.mode === 'jev' ? (
        <>
          <SpecRow
            fieldId="agentos_router.jev.api_key"
            inputId={`${id}-jev-key`}
            label={t('webSetConfig.jevApiKey')}
          >
            <Input
              id={`${id}-jev-key`}
              type="password"
              autoComplete="off"
              placeholder={t('webSetConfig.keepSecret')}
              value={d.jevApiKey}
              onChange={(e) => set({ jevApiKey: e.target.value })}
            />
          </SpecRow>
          {numberField(
            'agentos_router.jev.high_risk_threshold',
            t('webSetConfig.jevHighRisk'),
            d.jevHighRisk,
            t('webSetConfig.jevHighRiskHint'),
            'jevHighRisk',
          )}
        </>
      ) : null}
      {select(
        'agentos_router.translate_ceiling_tier',
        t('webSetConfig.translateCap'),
        d.translate,
        [{ value: 'off', label: t('webSetConfig.translateOff') }, ...tierOptions],
        (v) => set({ translate: v }),
        t('webSetConfig.translateHint'),
      )}

      {provider ? (
        <div className="mt-4 flex flex-col gap-2">
          <h3 className="text-label text-text-muted">{t('webSetConfig.tiers')}</h3>
          <div role="table" aria-label={t('webSetConfig.tiers')} className="set-grid">
            <div role="row" className="set-tiers-head set-grid-head">
              <span role="columnheader">{t('webSetConfig.tierColTier')}</span>
              <span role="columnheader">{t('webSetConfig.tierColModel')}</span>
              <span role="columnheader">{t('webSetConfig.tierColThinking')}</span>
              <span role="columnheader" className="text-end">
                {t('webSetConfig.tierImageShort')}
              </span>
            </div>
            {Object.entries(d.rows).map(([tier, row]) => {
              const image = tier === 'image_model'
              return (
                <div
                  key={tier}
                  role="row"
                  data-field-id={`agentos_router.tiers.${tier}.model`}
                  className="set-tiers-row"
                >
                  <div
                    role="cell"
                    data-col="tier"
                    className="flex min-w-0 flex-col items-start gap-1"
                  >
                    <code className="set-key font-medium text-text">{tier}</code>
                    {/* Read-only: requests always go through the configured provider. */}
                    <code
                      className="max-w-full truncate font-mono text-small text-text-muted"
                      aria-label={t('webSetConfig.tierProvider', { tier })}
                    >
                      {provider}
                    </code>
                  </div>
                  <div role="cell" data-col="model" className="min-w-0">
                    <Input
                      aria-label={t('webSetConfig.tierModel', { tier })}
                      className="font-mono text-small"
                      value={row.model}
                      title={row.model}
                      autoComplete="off"
                      onChange={(e) => setRow(tier, { model: e.target.value })}
                    />
                  </div>
                  <div role="cell" data-col="thinking" className="min-w-0">
                    <Select
                      aria-label={t('webSetConfig.tierThinking', { tier })}
                      value={row.thinkingLevel}
                      onChange={(e) => setRow(tier, { thinkingLevel: e.target.value })}
                    >
                      {THINKING_LEVELS.map((level) => (
                        <option key={level} value={level}>
                          {level || t('webSettings.optionDefault')}
                        </option>
                      ))}
                    </Select>
                  </div>
                  <label
                    role="cell"
                    data-col="image"
                    className="flex items-center gap-2 text-small"
                  >
                    <span aria-hidden className="set-tiers-image-label text-text-muted">
                      {t('webSetConfig.tierImageShort')}
                    </span>
                    <Switch
                      aria-label={t('webSetConfig.tierImage', { tier })}
                      checked={image || row.supportsImage}
                      disabled={image}
                      onCheckedChange={(v) => setRow(tier, { supportsImage: v })}
                    />
                  </label>
                </div>
              )
            })}
          </div>
        </div>
      ) : null}
      <ApplyBar
        snapshot={snapshot}
        busy={configure.busy}
        error={configure.error}
        disabled={!provider}
        onApply={submit}
      />
    </SettingsCard>
  )
}
