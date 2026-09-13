import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, Check, ExternalLink, Eye, EyeOff, LoaderCircle } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { configuredProvider, type ProviderSpec, type SetupConfig } from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { cn } from '~/lib/utils'
import { useGateway } from '~/stores/gateway'
import {
  customEndpointErrors,
  customEndpointPatch,
  customProviderSpec,
  isCustomProvider,
  isThinkingLevel,
  modelOptions,
  orderProviders,
  RECOMMENDED_PROVIDER,
  providerConfigurePayload,
  providerDirty,
  providerDraft,
  providerNeedsKey,
  providerState,
  THINKING_LEVELS,
  type CatalogModel,
  type ProviderDraft,
} from '../logic'
import { Card, Head, Notice, Pill, Row, Value } from '../parts'
import { providerKeyUrl } from '../provider-links'
import { ProviderLogo } from '../ProviderLogo'
import { useConfigSnapshot, withRevision } from '../use-snapshot'

interface ConfigureResult {
  restartRequired?: boolean
  warnings?: string[]
}
interface SetResult {
  restartRequired?: boolean
}
interface ProbeResult {
  ok: boolean
  models: { id: string; name?: string; contextWindow?: number }[]
  model?: string
  latencyMs?: number
  error: string | null
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/**
 * Who answers: a grid of every runtime-supported provider from the catalog,
 * one active at a time, and the selected one's credentials, connection and
 * default model underneath. Written through `onboarding.provider.configure`
 * with the snapshot revision, exactly as the console's setup does.
 */
export function ProvidersPane() {
  const { connected, query, snapshot, reload } = useConfigSnapshot()

  return (
    <>
      <Head title={t('settings.section.providers')} blurb={t('settings.section.providers.blurb')} />
      {!connected ? (
        <Notice tone="info">{t('settings.offline')}</Notice>
      ) : query.isError ? (
        <Notice tone="danger">{t('settings.loadFailed')}</Notice>
      ) : !snapshot ? (
        <div className="flex items-center gap-2 text-muted-foreground">
          <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
          {t('settings.loading')}
        </div>
      ) : (
        <ProvidersBody snapshot={snapshot} reload={reload} />
      )}
    </>
  )
}

function ProvidersBody({
  snapshot,
  reload,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
}) {
  const rpc = useRpc()
  const config: SetupConfig = snapshot.config ?? {}
  const catalogProviders = (snapshot.catalog?.providers ?? []).filter((p) => p.runtimeSupported)
  const providers = orderProviders(
    catalogProviders.some((p) => isCustomProvider(p.providerId))
      ? catalogProviders
      : [
          ...catalogProviders,
          customProviderSpec(
            t('settings.providers.custom.label'),
            t('settings.providers.custom.need'),
          ),
        ],
  )
  const configured = configuredProvider(snapshot.status ?? {}, config)
  const restartGateway = useGateway((s) => s.restart)
  // A gateway this app spawned is ours to restart: do it, rather than
  // handing the person a "restart to apply" chore.
  const managedGateway = useGateway((s) => s.status.pid !== null)
  const [selected, setSelected] = useState(configured)
  const selectedSpec = providers.find((p) => p.providerId === selected)

  const save = useMutation({
    mutationFn: (draft: ProviderDraft) =>
      isCustomProvider(draft.providerId)
        ? rpc.call<ConfigureResult>(
            'config.patch',
            withRevision(snapshot, customEndpointPatch(draft, configured === draft.providerId)),
          )
        : rpc.call<ConfigureResult>(
            'onboarding.provider.configure',
            withRevision(snapshot, providerConfigurePayload(draft)),
          ),
    onSuccess: async (res, draft) => {
      for (const w of res?.warnings ?? []) toast.warning(w)
      setSelected(draft.providerId)
      if (res?.restartRequired && managedGateway) {
        toast.success(t('settings.providers.savedRestarting'), { id: 'stg-provider' })
        await restartGateway()
      } else {
        toast.success(
          res?.restartRequired ? t('settings.restartRequired') : t('settings.providers.saved'),
          { id: 'stg-provider' },
        )
      }
      await reload()
    },
    onError: (err) =>
      toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: 'stg-provider-err' }),
  })

  const setThinking = useMutation({
    mutationFn: (value: string | null) =>
      rpc.call<SetResult>('config.set', withRevision(snapshot, { path: 'llm.thinking', value })),
    onSuccess: async (res) => {
      toast.success(res?.restartRequired ? t('settings.restartRequired') : t('settings.saved'), {
        id: 'stg-thinking',
      })
      await reload()
    },
    onError: (err) =>
      toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: 'stg-thinking-err' }),
  })

  const thinking = isThinkingLevel(config.llm?.thinking) ? config.llm.thinking : ''
  const blocked = Boolean(snapshot.writeBlocked)

  return (
    <>
      {snapshot.writeBlocked ? (
        <Notice tone="danger">{t('settings.writeBlocked')}</Notice>
      ) : snapshot.pendingRestart ? (
        <Notice
          action={
            <Button onClick={() => void restartGateway()}>{t('settings.restartGateway')}</Button>
          }
        >
          {t('settings.pendingRestart')}
        </Notice>
      ) : null}

      <div className="prov-grid" role="radiogroup" aria-label={t('settings.providers.pick')}>
        {providers.map((p) => {
          const state = providerState(p, config, configured)
          const isSelected = p.providerId === selected
          return (
            <button
              key={p.providerId}
              type="button"
              role="radio"
              aria-checked={isSelected}
              className={cn('prov-tile app-no-drag')}
              data-active={state.active ? 'true' : undefined}
              onClick={() => setSelected(p.providerId)}
            >
              <ProviderLogo id={p.providerId} label={p.label ?? p.providerId} />
              <span className="prov-tile__name">{p.label ?? p.providerId}</span>
              <span className="prov-tile__meta">
                {state.active ? (
                  <span className="prov-tile__state" data-tone="ok">
                    <Check className="size-3" strokeWidth={2.5} aria-hidden />
                    {t('settings.providers.state.active')}
                  </span>
                ) : state.profile ? (
                  <span className="prov-tile__state">{t('settings.providers.state.saved')}</span>
                ) : p.deployment === 'local' ? (
                  <span className="prov-tile__state">{t('settings.providers.state.local')}</span>
                ) : isCustomProvider(p.providerId) ? (
                  <span className="prov-tile__state">{t('settings.providers.custom.state')}</span>
                ) : (
                  <span className="prov-tile__state" data-tone="dim">
                    {t('settings.providers.state.needsKey')}
                  </span>
                )}
                {p.providerId === RECOMMENDED_PROVIDER ? (
                  <span className="prov-tile__tag" data-tone="recommended">
                    {t('settings.providers.state.recommended')}
                  </span>
                ) : null}
                {p.routerSupported ? (
                  <span className="prov-tile__tag">{t('settings.providers.state.router')}</span>
                ) : null}
              </span>
            </button>
          )
        })}
      </div>

      {selectedSpec ? (
        // Keyed on the selection + revision so a save or a switch re-seeds the form.
        <ProviderForm
          key={`${selectedSpec.providerId}:${snapshot.revision ?? ''}`}
          config={config}
          spec={selectedSpec}
          configured={configured}
          keyDetail={snapshot.status?.sectionDetails?.llm?.detail}
          saving={save.isPending}
          disabled={blocked}
          onSave={(draft) => save.mutate(draft)}
        />
      ) : null}

      <Card title={t('settings.models.thinking')} blurb={t('settings.models.thinking.help')}>
        <Row label={t('settings.models.thinking')}>
          <select
            className="mac-select"
            data-compact="true"
            aria-label={t('settings.models.thinking')}
            value={thinking}
            disabled={setThinking.isPending || blocked}
            onChange={(e) => setThinking.mutate(e.target.value || null)}
          >
            <option value="">{t('settings.models.thinking.auto')}</option>
            {THINKING_LEVELS.map((level) => (
              <option key={level} value={level}>
                {t(`settings.models.thinking.${level}`)}
              </option>
            ))}
          </select>
        </Row>
      </Card>
    </>
  )
}

export function ProviderForm({
  config,
  spec,
  configured,
  keyDetail,
  saving,
  disabled,
  onSave,
  submitLabel,
}: {
  config: SetupConfig
  spec: ProviderSpec
  configured: string
  keyDetail?: string
  saving: boolean
  disabled: boolean
  onSave: (draft: ProviderDraft) => void
  /** Overrides the primary button's label (first run: "Save and continue"). */
  submitLabel?: string
}) {
  const rpc = useRpc()
  const ids = { key: useId(), env: useId(), url: useId(), proxy: useId(), model: useId() }
  const saved = providerDraft(config, spec)
  const [draft, setDraft] = useState<ProviderDraft>(saved)
  const [showKey, setShowKey] = useState(false)
  const label = spec.label ?? spec.providerId
  const switching = spec.providerId !== configured
  const dirty = switching || providerDirty(saved, draft)
  const custom = isCustomProvider(spec.providerId)
  const needsKey = providerNeedsKey(draft, spec, config)
  const customErrors = custom ? customEndpointErrors(draft) : {}
  const invalid = Boolean(customErrors.baseUrl || customErrors.model)
  const state = providerState(spec, config, configured)
  const hasStoredKey = state.active && Boolean(config.llm?.api_key)
  const hasEnvKey =
    (state.active && Boolean(config.llm?.api_key_env) && !config.llm?.api_key) ||
    (!state.active && Boolean(state.profile?.api_key_env))

  const models = useQuery({
    queryKey: ['settings', 'models', spec.providerId],
    staleTime: 60_000,
    retry: false,
    queryFn: () => rpc.call<CatalogModel[]>('models.list', { provider: spec.providerId }),
  })
  const modelListId = useId()

  // "Test key": one round trip to the provider with the key as typed — its
  // model list, then a 1-token turn — so a wrong key shows up here, not on
  // the first message, and the model menu is the provider's own list.
  const probe = useMutation({
    mutationFn: () =>
      rpc.call<ProbeResult>('providers.probe', {
        providerId: spec.providerId,
        apiKey: draft.apiKey.trim() || undefined,
        apiKeyEnv: draft.apiKeyEnv.trim() || undefined,
        baseUrl: draft.baseUrl.trim() || undefined,
        model: draft.model.trim() || undefined,
      }),
  })
  const probed = probe.data
  const probeVerdict: 'idle' | 'checking' | 'ok' | 'bad' = probe.isPending
    ? 'checking'
    : probe.isError
      ? 'bad'
      : probed
        ? probed.ok
          ? 'ok'
          : 'bad'
        : 'idle'
  const probeError = probe.error
    ? probe.error instanceof Error
      ? probe.error.message
      : String(probe.error)
    : (probed?.error ?? '')
  // A stored or env key can be tried without typing anything: do it once,
  // so the model menu is populated the moment the form opens.
  const canAutoProbe = !custom && (hasStoredKey || hasEnvKey)
  const runProbe = probe.mutate
  useEffect(() => {
    if (canAutoProbe) runProbe()
  }, [canAutoProbe, runProbe])

  const providerModels: CatalogModel[] = (probed?.models ?? []).map((m) => ({
    id: m.id,
    name: m.name ?? m.id,
    provider: spec.providerId,
  }))
  const catalogModels = providerModels.length > 0 ? providerModels : (models.data ?? [])
  const options = modelOptions(catalogModels, spec.providerId, draft.model)
  // A provider without a key has no live catalog: nothing to check the model against.
  const hasCatalog = catalogModels.length > 0
  const keyUrl = providerKeyUrl(spec.providerId)

  const keyHelp = needsKey
    ? t('settings.providers.key.missing')
    : hasEnvKey
      ? `${t('settings.providers.key.env')} ${state.active ? (keyDetail ?? '') : ''}`.trim()
      : hasStoredKey
        ? t('settings.providers.key.saved')
        : undefined

  return (
    <Card
      title={label}
      icon={<ProviderLogo id={spec.providerId} label={label} size={36} />}
      blurb={
        <>
          {custom
            ? ''
            : spec.deployment === 'local'
              ? `${t('settings.providers.deployment.local')} `
              : `${t('settings.providers.deployment.cloud')} `}
          {spec.whatYouNeed?.join(' ') ?? ''}
          {keyUrl ? (
            <>
              {' '}
              <button
                type="button"
                className="prov-keylink"
                onClick={() => void desktopApi().app.openExternal(keyUrl)}
              >
                {t('settings.providers.key.get')}
                <ExternalLink className="size-3" strokeWidth={2} aria-hidden />
              </button>
            </>
          ) : null}
        </>
      }
      action={
        <span className="flex items-center gap-2">
          {state.active ? <Pill tone="ok">{t('settings.providers.state.active')}</Pill> : null}
          {spec.routerSupported ? (
            <Pill tone="primary">{t('settings.providers.routerOk')}</Pill>
          ) : (
            <Pill>{t('settings.providers.directOnly')}</Pill>
          )}
        </span>
      }
      footNote={
        switching
          ? custom
            ? t('settings.providers.custom.switchNote')
            : t('settings.providers.switchNote')
          : undefined
      }
      foot={
        <>
          <Button
            disabled={!providerDirty(saved, draft) || saving}
            onClick={() => {
              setDraft(saved)
              setShowKey(false)
            }}
          >
            {t('settings.revert')}
          </Button>
          <Button
            variant="primary"
            disabled={!dirty || saving || needsKey || invalid || disabled}
            onClick={() => onSave(draft)}
          >
            {submitLabel ??
              (switching ? `${t('settings.providers.switchTo')} ${label}` : t('settings.save'))}
          </Button>
        </>
      }
    >
      {custom ? (
        <Row
          label={t('settings.providers.baseUrl')}
          htmlFor={ids.url}
          help={
            customErrors.baseUrl && draft.baseUrl ? (
              <span className="stg-error">{t('settings.providers.custom.baseUrl.invalid')}</span>
            ) : (
              t('settings.providers.custom.baseUrl.help')
            )
          }
          stack
        >
          <input
            id={ids.url}
            className="mac-input"
            data-mono="true"
            data-invalid={customErrors.baseUrl && draft.baseUrl ? 'true' : undefined}
            autoComplete="off"
            spellCheck={false}
            placeholder="http://localhost:8000/v1"
            value={draft.baseUrl}
            disabled={disabled}
            onChange={(e) => setDraft((d) => ({ ...d, baseUrl: e.target.value }))}
          />
        </Row>
      ) : null}

      {spec.requiresApiKey || custom ? (
        <Row
          label={t('settings.providers.key')}
          htmlFor={ids.key}
          help={
            <>
              {custom && !keyHelp ? (
                t('settings.providers.custom.key.help')
              ) : keyHelp ? (
                <span className={needsKey ? 'stg-error' : undefined}>{keyHelp}</span>
              ) : null}
              {probeVerdict !== 'idle' ? (
                <span className="prov-probe" data-verdict={probeVerdict} data-testid="key-probe">
                  {probeVerdict === 'checking' ? (
                    <>
                      <LoaderCircle className="stg-spin size-3" strokeWidth={2} aria-hidden />
                      {t('settings.providers.key.testing')}
                    </>
                  ) : probeVerdict === 'ok' ? (
                    <>
                      <Check className="size-3" strokeWidth={2.5} aria-hidden />
                      {t('settings.providers.key.works')}
                      {probed?.models.length
                        ? ` ${probed.models.length} ${t('settings.providers.key.worksModels')}`
                        : ''}
                    </>
                  ) : (
                    <>
                      <AlertTriangle className="size-3" strokeWidth={2} aria-hidden />
                      {t('settings.providers.key.failed')} <code>{probeError}</code>
                    </>
                  )}
                </span>
              ) : null}
            </>
          }
          align="start"
        >
          <span className="stg-input-wrap">
            <input
              id={ids.key}
              className="mac-input"
              data-mono="true"
              type={showKey ? 'text' : 'password'}
              autoComplete="off"
              spellCheck={false}
              placeholder={
                hasStoredKey || hasEnvKey
                  ? t('settings.providers.key.placeholder.saved')
                  : t('settings.providers.key.placeholder.new')
              }
              value={draft.apiKey}
              disabled={disabled}
              onChange={(e) => setDraft((d) => ({ ...d, apiKey: e.target.value }))}
            />
            <Button
              variant="ghost"
              size="icon"
              aria-label={
                showKey ? t('settings.providers.key.hide') : t('settings.providers.key.show')
              }
              aria-pressed={showKey}
              onClick={() => setShowKey((v) => !v)}
            >
              {showKey ? (
                <EyeOff className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
              ) : (
                <Eye className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
              )}
            </Button>
          </span>
          <Button
            disabled={
              disabled ||
              probe.isPending ||
              (Boolean(spec.requiresApiKey) && !draft.apiKey.trim() && !hasStoredKey && !hasEnvKey)
            }
            onClick={() => probe.mutate()}
            data-testid="key-probe-button"
          >
            {probe.isPending
              ? t('settings.providers.key.testing')
              : t('settings.providers.key.test')}
          </Button>
        </Row>
      ) : null}

      {custom ? (
        <Row
          label={t('settings.providers.custom.model')}
          htmlFor={ids.model}
          help={
            customErrors.model && draft.baseUrl.trim() ? (
              <span className="stg-error">{t('settings.providers.custom.model.missing')}</span>
            ) : (
              t('settings.providers.custom.model.help')
            )
          }
        >
          <input
            id={ids.model}
            className="mac-input"
            data-mono="true"
            autoComplete="off"
            spellCheck={false}
            list={hasCatalog ? modelListId : undefined}
            placeholder="llama-3.3-70b"
            value={draft.model}
            disabled={disabled}
            onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))}
          />
          {hasCatalog ? (
            <datalist id={modelListId}>
              {options.map((opt) => (
                <option key={opt.id} value={opt.id} />
              ))}
            </datalist>
          ) : null}
        </Row>
      ) : null}

      {!custom ? (
        <Row
          label={t('settings.providers.model')}
          help={
            <>
              <span>{t('settings.providers.model.help')}</span>
              {hasCatalog ? (
                <span>
                  {catalogModels.length} {t('settings.providers.catalog')}
                  {providerModels.length > 0
                    ? ` ${t('settings.providers.model.fromProvider')}`
                    : ''}
                </span>
              ) : null}
            </>
          }
          align="start"
        >
          <select
            className="mac-select"
            aria-label={t('settings.providers.model')}
            value={draft.model}
            disabled={disabled || models.isPending}
            onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))}
          >
            <option value="">{t('settings.providers.model.none')}</option>
            {options.map((opt) => (
              <option key={opt.id} value={opt.id} title={opt.title || undefined}>
                {opt.custom && hasCatalog
                  ? `${opt.label}  (${t('settings.providers.model.custom')})`
                  : opt.label}
              </option>
            ))}
          </select>
        </Row>
      ) : null}

      <details className="stg-details">
        <summary className="stg-row stg-details__summary">
          <span className="stg-row__label">
            <span>{t('settings.providers.connection')}</span>
            <span className="stg-row__help">
              <Value>
                {custom ? draft.proxy || '—' : draft.baseUrl || spec.defaultBaseUrl || '—'}
              </Value>
            </span>
          </span>
        </summary>
        {!custom ? (
          <Row label={t('settings.providers.baseUrl')} htmlFor={ids.url} stack>
            <input
              id={ids.url}
              className="mac-input"
              data-mono="true"
              autoComplete="off"
              spellCheck={false}
              placeholder={spec.defaultBaseUrl || ''}
              value={draft.baseUrl}
              disabled={disabled}
              onChange={(e) => setDraft((d) => ({ ...d, baseUrl: e.target.value }))}
            />
          </Row>
        ) : null}
        {spec.requiresApiKey ? (
          <Row
            label={t('settings.providers.keyEnv')}
            help={t('settings.providers.keyEnv.help')}
            htmlFor={ids.env}
          >
            <input
              id={ids.env}
              className="mac-input"
              data-mono="true"
              autoComplete="off"
              spellCheck={false}
              placeholder={spec.envKey || ''}
              value={draft.apiKeyEnv}
              disabled={disabled || Boolean(draft.apiKey.trim())}
              onChange={(e) => setDraft((d) => ({ ...d, apiKeyEnv: e.target.value }))}
            />
          </Row>
        ) : null}
        <Row
          label={t('settings.providers.proxy')}
          help={t('settings.providers.proxy.help')}
          htmlFor={ids.proxy}
        >
          <input
            id={ids.proxy}
            className="mac-input"
            data-mono="true"
            autoComplete="off"
            spellCheck={false}
            placeholder="http://127.0.0.1:7890"
            value={draft.proxy}
            disabled={disabled}
            onChange={(e) => setDraft((d) => ({ ...d, proxy: e.target.value }))}
          />
        </Row>
      </details>
    </Card>
  )
}
