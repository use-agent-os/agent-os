import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Eye, EyeOff, Loader2, TriangleAlert } from 'lucide-react'
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
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { Switch } from '@/web/components/ui/switch'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { afterConfigWrite } from '../config-editor'
import { useReveal } from '../fields'
import { WriteBlockedNotice } from '../SaveBar'
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
      <LoadingRegion>
        <Skeleton className="h-32" />
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
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <Button
        variant="primary"
        size="sm"
        disabled={busy || blocked.disabled || Boolean(snapshot.writeBlocked) || disabled}
        onClick={onApply}
      >
        {busy ? <Loader2 className="animate-spin" /> : null}
        {busy ? t('webSetConfig.applying') : t('webSetConfig.apply')}
      </Button>
      {blocked.reason ? <span className="text-small text-text-muted">{blocked.reason}</span> : null}
      {error ? (
        <span role="alert" className="text-small text-danger">
          {error}
        </span>
      ) : null}
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
        <div
          role="status"
          className="mb-3 flex flex-wrap items-center gap-2 rounded-md border border-warn/50 bg-warn/10 px-3 py-2"
        >
          <TriangleAlert className="size-4 text-warn" aria-hidden data-status-icon />
          <span className="min-w-0 flex-1">{t('webSetConfig.changedElsewhere')}</span>
          <Button size="sm" variant="outline" onClick={onReload}>
            {t('webSettings.reload')}
          </Button>
        </div>
      ) : null}
    </>
  )
}

const inputClass =
  'h-9 w-full max-w-xl rounded-md border border-border-control bg-surface-0 px-3 touch:h-11 touch:text-base'

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
      <select
        id={id}
        aria-describedby={describedBy}
        className={inputClass}
        value={String(value ?? '')}
        onChange={(e) => onChange(e.target.value)}
      >
        {field.choices.map((c) => (
          <option key={c} value={c}>
            {c === '' ? t('webSettings.optionDefault') : c}
          </option>
        ))}
      </select>
    )
  } else {
    control = (
      <div className="flex max-w-xl items-center gap-2">
        <input
          id={id}
          aria-describedby={describedBy}
          type={
            secret && !shown
              ? 'password'
              : field.type === 'int' || field.type === 'float'
                ? 'number'
                : 'text'
          }
          autoComplete="off"
          placeholder={secret ? t('webSetConfig.keepSecret') : field.placeholder}
          className={inputClass}
          value={value === undefined || value === null ? '' : String(value)}
          onChange={(e) => onChange(e.target.value)}
        />
        {secret ? (
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={shown ? t('webSettings.hide') : t('webSettings.reveal')}
            onClick={toggle}
          >
            {shown ? <EyeOff /> : <Eye />}
          </Button>
        ) : null}
      </div>
    )
  }
  return (
    <div data-field-id={fieldId} className="flex flex-col gap-1 py-2">
      <label htmlFor={id} className="font-medium">
        {label}
        {field.required && !secret ? (
          <span className="ml-1 text-small text-text-muted">({t('webSetConfig.required')})</span>
        ) : null}
      </label>
      {help ? (
        <p id={helpId} className="text-small text-text-muted">
          {help}
        </p>
      ) : null}
      {control}
    </div>
  )
}

/** A copyable CLI command (env recovery for a capability). */
function CommandLine({ command }: { command: string }) {
  return (
    <div className="mt-2 flex max-w-xl items-center gap-1 rounded-md border border-border bg-surface-0 pl-3">
      <code
        tabIndex={0}
        className="min-w-0 flex-1 overflow-x-auto py-1.5 font-mono text-small whitespace-pre"
      >
        {command}
      </code>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label={t('webSetConfig.copyCommand')}
        onClick={() =>
          void copyWithFallback(command)
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
    <section aria-label={title} className="rounded-lg border border-border p-4">
      <h2 className="mb-1 font-semibold">{title}</h2>
      {status ? <p className="mb-2 text-small text-text-muted">{status}</p> : null}
      {command ? <CommandLine command={command} /> : null}
      <FormNotices snapshot={snapshot} stale={draft.stale} onReload={draft.reload} />
      {children}
      {!adapter.single ? (
        <div data-field-id={adapter.providerPath} className="flex flex-col gap-1 py-2">
          <label htmlFor={selectId} className="font-medium">
            {t('webSetConfig.provider')}
          </label>
          <select
            id={selectId}
            className={inputClass}
            value={providerId}
            onChange={(e) => switchProvider(e.target.value)}
          >
            {specs.map((s) => (
              <option key={s.providerId} value={s.providerId}>
                {s.label || s.providerId}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {aside?.(spec)}
      {spec?.whatYouNeed?.length ? (
        <p className="mt-1 text-small text-text-muted">
          {t('webSetConfig.whatYouNeed', { items: needsSentence(spec.whatYouNeed) })}
        </p>
      ) : null}
      {core.map(renderField)}
      {advanced.length ? (
        <details className="mt-2" open={advancedOpen || undefined}>
          <summary className="target cursor-pointer font-medium">
            {t('webSetConfig.advancedFields')}
          </summary>
          <div className="pl-1">{advanced.map(renderField)}</div>
        </details>
      ) : null}
      <ApplyBar
        snapshot={snapshot}
        busy={configure.busy}
        error={configure.error}
        disabled={!spec}
        onApply={submit}
      />
    </section>
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
    <div data-field-id={fieldId} className="flex flex-col gap-1 py-2">
      <label htmlFor={`${id}-${fieldId}`} className="font-medium">
        {label}
      </label>
      <select
        id={`${id}-${fieldId}`}
        className={inputClass}
        value={value}
        aria-describedby={hint ? `${id}-${fieldId}-hint` : undefined}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {hint ? (
        <p id={`${id}-${fieldId}-hint`} className="text-small text-text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  )
  const numberField = (
    fieldId: string,
    label: string,
    value: string,
    hint: string,
    key: keyof RouterDraft,
  ) => (
    <div data-field-id={fieldId} className="flex flex-col gap-1 py-2">
      <label htmlFor={`${id}-${fieldId}`} className="font-medium">
        {label}
      </label>
      <input
        id={`${id}-${fieldId}`}
        type="number"
        min={0}
        max={1}
        step={0.05}
        className={inputClass}
        value={value}
        aria-describedby={`${id}-${fieldId}-hint`}
        onChange={(e) => set({ [key]: e.target.value } as Partial<RouterDraft>)}
      />
      <p id={`${id}-${fieldId}-hint`} className="text-small text-text-muted">
        {hint}
      </p>
    </div>
  )
  const tierOptions = TEXT_TIERS.map((tier) => ({ value: tier, label: tier }))

  return (
    <section aria-label={t('webSetConfig.router')} className="rounded-lg border border-border p-4">
      <h2 className="font-semibold">{t('webSetConfig.router')}</h2>
      <p className="mb-2 text-small text-text-muted">{t('webSetConfig.routerHelp')}</p>
      <FormNotices snapshot={snapshot} stale={draft.stale} onReload={draft.reload} />
      {!provider ? (
        <p role="status" className="text-text-muted">
          {t('webSetConfig.routerNeedsProvider')}
        </p>
      ) : null}
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
          <div data-field-id="agentos_router.jev.api_key" className="flex flex-col gap-1 py-2">
            <label htmlFor={`${id}-jev-key`} className="font-medium">
              {t('webSetConfig.jevApiKey')}
            </label>
            <input
              id={`${id}-jev-key`}
              type="password"
              autoComplete="off"
              className={inputClass}
              placeholder={t('webSetConfig.keepSecret')}
              value={d.jevApiKey}
              onChange={(e) => set({ jevApiKey: e.target.value })}
            />
          </div>
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
        <div role="table" aria-label={t('webSetConfig.tiers')} className="mt-3 flex flex-col gap-2">
          {Object.entries(d.rows).map(([tier, row]) => {
            const image = tier === 'image_model'
            return (
              <div
                key={tier}
                role="row"
                data-field-id={`agentos_router.tiers.${tier}.model`}
                className="grid gap-2 rounded-md border border-border p-2 sm:grid-cols-[5rem_1fr_9rem_auto] sm:items-center"
              >
                <div role="cell" className="flex flex-col">
                  <code className="font-medium">{tier}</code>
                  {/* Read-only: requests always go through the configured provider. */}
                  <code
                    className="text-small text-text-muted"
                    aria-label={t('webSetConfig.tierProvider', { tier })}
                  >
                    {provider}
                  </code>
                </div>
                <div role="cell">
                  <input
                    aria-label={t('webSetConfig.tierModel', { tier })}
                    className={inputClass}
                    value={row.model}
                    title={row.model}
                    autoComplete="off"
                    onChange={(e) => setRow(tier, { model: e.target.value })}
                  />
                </div>
                <div role="cell">
                  <select
                    aria-label={t('webSetConfig.tierThinking', { tier })}
                    className={cn(inputClass, 'w-full')}
                    value={row.thinkingLevel}
                    onChange={(e) => setRow(tier, { thinkingLevel: e.target.value })}
                  >
                    {THINKING_LEVELS.map((level) => (
                      <option key={level} value={level}>
                        {level || t('webSettings.optionDefault')}
                      </option>
                    ))}
                  </select>
                </div>
                <label role="cell" className="flex items-center gap-2 text-small">
                  <Switch
                    aria-label={t('webSetConfig.tierImage', { tier })}
                    checked={image || row.supportsImage}
                    disabled={image}
                    onCheckedChange={(v) => setRow(tier, { supportsImage: v })}
                  />
                  <span aria-hidden>{t('webSetConfig.tierImageShort')}</span>
                </label>
              </div>
            )
          })}
        </div>
      ) : null}
      <ApplyBar
        snapshot={snapshot}
        busy={configure.busy}
        error={configure.error}
        disabled={!provider}
        onApply={submit}
      />
    </section>
  )
}
