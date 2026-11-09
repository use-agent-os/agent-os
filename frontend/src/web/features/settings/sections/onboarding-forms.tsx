import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webSetConfig'
import '@/i18n/en/webSettings'
import { gate, requireConnection } from '@/lib/disabled-reason'
import {
  loadSettingsSnapshot,
  SETTINGS_SNAPSHOT_QUERY_KEY,
  type SettingsSnapshot,
} from '@/views/settings/snapshot'
import {
  buildRouterConfigureParams,
  camel,
  mergeTiers,
  routerMode,
  TEXT_TIERS,
  type FieldSpec,
  type ProviderSpec,
  type RouterMode,
} from '@/views/setup/logic'
import { Button } from '@/web/components/ui/button'
import { ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useDisplayedConnection } from '@/web/shell/use-displayed-connection'
import { CONFIG_SNAPSHOT_KEY, useRestartNotice } from '../config-editor'
import { useReveal } from '../fields'

// Forms driven by the onboarding catalog (Models & routing, Search, Media and
// the guided setup share them). Each submits one onboarding.*.configure call
// with the revision the snapshot was read at, like the previous console.

export function useSetupSnapshot() {
  const rpc = useRpc()
  return useQuery({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
  })
}

interface ConfigureResult {
  restartRequired?: boolean
  restartReasons?: string[]
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
  const run = async (method: string, params: Record<string, unknown>, revision?: string | null) => {
    setBusy(true)
    setError(null)
    try {
      const result = await rpc.call<ConfigureResult>(method, {
        ...params,
        ...(revision ? { expectedRevision: revision } : {}),
      })
      if (result?.restartRequired) useRestartNotice.getState().show(result.restartReasons ?? [])
      toast.success(t('webSetConfig.applied'))
      await queryClient.invalidateQueries({ queryKey: SETTINGS_SNAPSHOT_QUERY_KEY })
      await queryClient.invalidateQueries({ queryKey: CONFIG_SNAPSHOT_KEY })
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

const inputClass =
  'h-9 w-full max-w-xl rounded-md border border-border-control bg-surface-0 px-3 touch:h-11 touch:text-base'

function SpecField({
  field,
  value,
  onChange,
}: {
  field: FieldSpec
  value: unknown
  onChange(value: unknown): void
}) {
  const id = useId()
  const [shown, toggle] = useReveal()
  const label = field.label || field.name
  const secret = field.secret || field.type === 'password'
  let control: React.ReactNode
  if (field.type === 'bool') {
    control = (
      <input
        id={id}
        type="checkbox"
        checked={Boolean(value)}
        onChange={(e) => onChange(e.target.checked)}
      />
    )
  } else if (field.type === 'select' && field.choices) {
    control = (
      <select
        id={id}
        className={inputClass}
        value={String(value ?? '')}
        onChange={(e) => onChange(e.target.value)}
      >
        {field.choices.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    )
  } else {
    control = (
      <div className="flex max-w-xl items-center gap-2">
        <input
          id={id}
          type={
            secret && !shown
              ? 'password'
              : field.type === 'int' || field.type === 'float'
                ? 'number'
                : 'text'
          }
          autoComplete="off"
          placeholder={field.placeholder}
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
    <div data-field-id={field.name} className="flex flex-col gap-1 py-2">
      <label htmlFor={id} className="font-medium">
        {label}
        {field.required ? (
          <span className="ml-1 text-small text-text-muted">({t('webSetConfig.required')})</span>
        ) : null}
      </label>
      {field.description || field.help ? (
        <p className="text-small text-text-muted">{field.description || field.help}</p>
      ) : null}
      {control}
    </div>
  )
}

/**
 * Choose one spec from a catalog list and fill its fields. Secrets left blank
 * keep the stored value (the gateway's rule), so they start empty.
 */
export function SpecForm({
  title,
  specs,
  current,
  method,
  revision,
  buildParams,
}: {
  title: string
  specs: ProviderSpec[]
  current: string
  method: string
  revision?: string | null
  buildParams?(providerId: string, values: Record<string, unknown>): Record<string, unknown>
}) {
  const [providerId, setProviderId] = useState(current || specs[0]?.providerId || '')
  const [values, setValues] = useState<Record<string, unknown>>({})
  const configure = useConfigure()
  const connection = useDisplayedConnection()
  const blocked = gate(requireConnection(connection))
  const spec = specs.find((s) => s.providerId === providerId)
  const selectId = useId()
  const visible = (spec?.fields ?? []).filter((f) =>
    Object.entries(f.showWhen ?? {}).every(([k, v]) => (values[k] ?? '') === v),
  )
  const submit = () => {
    const filled = Object.fromEntries(
      Object.entries(values).filter(([name, v]) => {
        const field = spec?.fields?.find((f) => f.name === name)
        return !((field?.secret || field?.type === 'password') && v === '')
      }),
    )
    const params = buildParams
      ? buildParams(providerId, filled)
      : { providerId, ...Object.fromEntries(Object.entries(filled).map(([k, v]) => [camel(k), v])) }
    void configure.run(method, params, revision)
  }
  return (
    <section aria-label={title} className="rounded-lg border border-border p-4">
      <h2 className="mb-2 font-semibold">{title}</h2>
      <label htmlFor={selectId} className="font-medium">
        {t('webSetConfig.provider')}
      </label>
      <select
        id={selectId}
        className={`${inputClass} mt-1`}
        value={providerId}
        onChange={(e) => {
          setProviderId(e.target.value)
          setValues({})
        }}
      >
        {specs.map((s) => (
          <option key={s.providerId} value={s.providerId}>
            {s.label || s.providerId}
          </option>
        ))}
      </select>
      {spec?.whatYouNeed?.length ? (
        <p className="mt-1 text-small text-text-muted">
          {t('webSetConfig.whatYouNeed', { items: spec.whatYouNeed.join(', ') })}
        </p>
      ) : null}
      {visible.map((field) => (
        <SpecField
          key={field.name}
          field={field}
          value={values[field.name] ?? (field.secret ? '' : field.default)}
          onChange={(v) => setValues((prev) => ({ ...prev, [field.name]: v }))}
        />
      ))}
      <div className="mt-3 flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={configure.busy || blocked.disabled || !providerId}
          onClick={submit}
        >
          {configure.busy ? <Loader2 className="animate-spin" /> : null}
          {configure.busy ? t('webSetConfig.applying') : t('webSetConfig.apply')}
        </Button>
        {blocked.reason ? (
          <span className="text-small text-text-muted">{blocked.reason}</span>
        ) : null}
        {configure.error ? (
          <span role="alert" className="text-small text-danger">
            {configure.error}
          </span>
        ) : null}
      </div>
    </section>
  )
}

/** Pilot Router mode, default tier and per-tier models. */
export function RouterForm({ snapshot }: { snapshot: SettingsSnapshot }) {
  const router = snapshot.config?.agentos_router ?? {}
  const profiles = snapshot.catalog?.routerProfiles
  const provider = snapshot.config?.llm?.provider ?? ''
  const merged = useMemo(() => {
    const profileTiers = profiles?.profiles?.find((p) => p.providerId === provider)?.tiers ?? {}
    return mergeTiers(profileTiers, router.tiers ?? {})
  }, [profiles, provider, router.tiers])
  const [mode, setMode] = useState<RouterMode>(routerMode(router))
  const [defaultTier, setDefaultTier] = useState(
    router.default_tier || profiles?.defaultTier || 'c1',
  )
  const [models, setModels] = useState<Record<string, string>>(() =>
    Object.fromEntries(TEXT_TIERS.map((tier) => [tier, String(merged[tier]?.model ?? '')])),
  )
  const configure = useConfigure()
  const connection = useDisplayedConnection()
  const blocked = gate(requireConnection(connection))
  const modeId = useId()
  const tierId = useId()

  const submit = () => {
    const params = buildRouterConfigureParams({
      sel: mode,
      defaultTier,
      judgeModel: router.judge_model ?? null,
      pilotThresholdRaw: undefined,
      translateCeilingEnabled: Boolean(router.translate_ceiling_enabled),
      translateCeilingTier: router.translate_ceiling_tier || 'c2',
      tiers: TEXT_TIERS.map((tier) => ({
        tier,
        provider: String(merged[tier]?.provider ?? provider),
        model: models[tier] ?? '',
        thinkingLevel: String(merged[tier]?.thinkingLevel ?? merged[tier]?.thinking_level ?? 'off'),
        supportsImage: Boolean(merged[tier]?.supportsImage ?? merged[tier]?.supports_image),
      })),
    })
    void configure.run(
      'onboarding.router.configure',
      params as unknown as Record<string, unknown>,
      snapshot.revision,
    )
  }

  return (
    <section aria-label={t('webSetConfig.router')} className="rounded-lg border border-border p-4">
      <h2 className="font-semibold">{t('webSetConfig.router')}</h2>
      <p className="mb-2 text-small text-text-muted">{t('webSetConfig.routerHelp')}</p>
      <label htmlFor={modeId} className="font-medium">
        {t('webSetConfig.routerMode')}
      </label>
      <select
        id={modeId}
        className={`${inputClass} mt-1`}
        value={mode}
        onChange={(e) => setMode(e.target.value as RouterMode)}
      >
        <option value="pilot-v1">{t('webSetConfig.routerModePilot')}</option>
        <option value="llm_judge">{t('webSetConfig.routerModeJudge')}</option>
        <option value="jev">{t('webSetConfig.routerModeJev')}</option>
        <option value="disabled">{t('webSetConfig.routerModeDisabled')}</option>
      </select>
      <label htmlFor={tierId} className="mt-3 block font-medium">
        {t('webSetConfig.defaultTier')}
      </label>
      <select
        id={tierId}
        className={`${inputClass} mt-1`}
        value={defaultTier}
        onChange={(e) => setDefaultTier(e.target.value)}
      >
        {TEXT_TIERS.map((tier) => (
          <option key={tier} value={tier}>
            {tier}
          </option>
        ))}
      </select>
      {TEXT_TIERS.map((tier) => (
        <div
          key={tier}
          data-field-id={`agentos_router.tiers.${tier}.model`}
          className="mt-2 flex flex-col gap-1"
        >
          <label htmlFor={`${tierId}-${tier}`} className="font-medium">
            {t('webSetConfig.tierModel', { tier })}
          </label>
          <input
            id={`${tierId}-${tier}`}
            className={inputClass}
            value={models[tier] ?? ''}
            onChange={(e) => setModels((prev) => ({ ...prev, [tier]: e.target.value }))}
          />
        </div>
      ))}
      <div className="mt-3 flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={configure.busy || blocked.disabled}
          onClick={submit}
        >
          {configure.busy ? t('webSetConfig.applying') : t('webSetConfig.apply')}
        </Button>
        {blocked.reason ? (
          <span className="text-small text-text-muted">{blocked.reason}</span>
        ) : null}
        {configure.error ? (
          <span role="alert" className="text-small text-danger">
            {configure.error}
          </span>
        ) : null}
      </div>
    </section>
  )
}
