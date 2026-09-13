import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowLeft, Check, LoaderCircle } from 'lucide-react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { configuredProvider, type SetupConfig } from '@/views/setup/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useBootstrap } from '~/stores/bootstrap'
import { useGateway } from '~/stores/gateway'
import {
  customEndpointPatch,
  customProviderSpec,
  isCustomProvider,
  orderProviders,
  providerConfigurePayload,
  providerState,
  RECOMMENDED_PROVIDER,
  type ProviderDraft,
} from '~/views/settings/logic'
import { ProviderForm } from '~/views/settings/panes/ProvidersPane'
import { ProviderLogo } from '~/views/settings/ProviderLogo'
import { useConfigSnapshot, withRevision } from '~/views/settings/use-snapshot'

interface ConfigureResult {
  restartRequired?: boolean
  warnings?: string[]
}

/**
 * Step 2 of the first run, as three screens instead of one long page:
 * the grid (pick), the form (one provider, the key), then "all set". Going
 * back is a link, not scrolling. Saving restarts a gateway this app manages
 * by itself, so the person never sees a "restart to apply" chore.
 */
export function ProviderStep({ onDone }: { onDone: () => void }) {
  const { snapshot, reload } = useConfigSnapshot()
  const rpc = useRpc()
  const restartGateway = useGateway((s) => s.restart)
  const managedGateway = useGateway((s) => s.status.pid !== null)
  const selected = useBootstrap((s) => s.provider.selected)
  const savedProvider = useBootstrap((s) => s.provider.saved)
  const setProvider = useBootstrap((s) => s.setProvider)
  const setSelected = (id: string | null) => setProvider({ selected: id })

  const save = useMutation({
    mutationFn: (draft: ProviderDraft) => {
      const configured = configuredProvider(snapshot?.status ?? {}, snapshot?.config ?? {})
      return isCustomProvider(draft.providerId)
        ? rpc.call<ConfigureResult>(
            'config.patch',
            withRevision(snapshot, customEndpointPatch(draft, configured === draft.providerId)),
          )
        : rpc.call<ConfigureResult>(
            'onboarding.provider.configure',
            withRevision(snapshot, providerConfigurePayload(draft)),
          )
    },
    onSuccess: async (res, draft) => {
      for (const w of res?.warnings ?? []) toast.warning(w)
      // Recorded before the restart: the reconnect remounts this step, and it
      // must come back on "all set", not on the grid.
      setProvider({ saved: draft.providerId })
      if (res?.restartRequired && managedGateway) {
        await restartGateway()
      } else if (res?.restartRequired) {
        toast.warning(t('setup.provider.savedRestart'), { id: 'setup-provider' })
      }
      await reload()
    },
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : String(err), { id: 'setup-provider-err' }),
  })

  if (savedProvider !== null) {
    const label =
      snapshot?.catalog?.providers?.find((p) => p.providerId === savedProvider)?.label ??
      savedProvider
    return (
      <Ready
        providerId={savedProvider}
        provider={label}
        onDone={onDone}
        onEditKey={() => setProvider({ saved: null, selected: savedProvider })}
      />
    )
  }

  if (!snapshot) {
    return (
      <p className="setup__blurb setup__blurb--row">
        <LoaderCircle className="setup__spin size-4" aria-hidden />
        {t('setup.provider.loading')}
      </p>
    )
  }

  const config: SetupConfig = snapshot.config ?? {}
  const configured = configuredProvider(snapshot.status ?? {}, config)
  const catalog = (snapshot.catalog?.providers ?? []).filter((p) => p.runtimeSupported)
  const providers = orderProviders(
    catalog.some((p) => isCustomProvider(p.providerId))
      ? catalog
      : [
          ...catalog,
          customProviderSpec(
            t('settings.providers.custom.label'),
            t('settings.providers.custom.need'),
          ),
        ],
  )
  const spec = providers.find((p) => p.providerId === selected)

  if (spec) {
    return (
      <div className="setup__form" data-testid="setup-provider-form">
        <button type="button" className="setup__back" onClick={() => setSelected(null)}>
          <ArrowLeft className="size-3.5" aria-hidden />
          {t('setup.provider.back')}
        </button>
        {save.isPending ? (
          <p className="setup__blurb setup__blurb--row">
            <LoaderCircle className="setup__spin size-4" aria-hidden />
            {t('setup.provider.saving')}
          </p>
        ) : null}
        <div className="stg">
          <ProviderForm
            key={`${spec.providerId}:${snapshot.revision ?? ''}`}
            config={config}
            spec={spec}
            configured={configured}
            keyDetail={snapshot.status?.sectionDetails?.llm?.detail}
            saving={save.isPending}
            disabled={Boolean(snapshot.writeBlocked)}
            onSave={(draft) => save.mutate(draft)}
            submitLabel={t('settings.providers.saveContinue')}
          />
        </div>
      </div>
    )
  }

  return (
    <>
      <h1 className="setup__title">{t('setup.provider.title')}</h1>
      <p className="setup__blurb">{t('setup.provider.blurb')}</p>
      <div className="setup__grid" data-testid="setup-provider-grid">
        {providers.map((p) => {
          const state = providerState(p, config, configured)
          const label = p.label ?? p.providerId
          return (
            <button
              key={p.providerId}
              type="button"
              className="setup__tile"
              data-testid={`setup-tile-${p.providerId}`}
              data-recommended={p.providerId === RECOMMENDED_PROVIDER ? 'true' : undefined}
              onClick={() => setSelected(p.providerId)}
            >
              <ProviderLogo id={p.providerId} label={label} />
              <span className="setup__tile-name">{label}</span>
              <span className="setup__tile-meta">
                {state.active ? (
                  <span data-tone="ok">
                    <Check className="size-3" strokeWidth={2.5} aria-hidden />
                    {t('settings.providers.state.active')}
                  </span>
                ) : p.deployment === 'local' ? (
                  t('settings.providers.state.local')
                ) : isCustomProvider(p.providerId) ? (
                  t('settings.providers.custom.state')
                ) : (
                  t('settings.providers.state.needsKey')
                )}
              </span>
              {p.providerId === RECOMMENDED_PROVIDER ? (
                <span className="setup__tile-badge">
                  {t('settings.providers.state.recommended')}
                </span>
              ) : null}
            </button>
          )
        })}
      </div>
      <div className="setup__actions setup__actions--end">
        <button type="button" className="setup__link setup__link--quiet" onClick={onDone}>
          {t('setup.provider.later')}
        </button>
      </div>
    </>
  )
}

interface ProbeResult {
  ok: boolean
  models: { id: string; name?: string }[]
  error: string | null
}

/**
 * "All set", with the key actually tried: `providers.probe` lists the
 * provider's models and sends one token with the saved key, so a wrong key
 * shows up here instead of on the first message.
 */
function Ready({
  providerId,
  provider,
  onDone,
  onEditKey,
}: {
  providerId: string
  provider: string | null
  onDone: () => void
  onEditKey: () => void
}) {
  const rpc = useRpc()
  const probe = useQuery({
    queryKey: ['setup', 'provider-probe', providerId],
    retry: false,
    staleTime: Infinity,
    queryFn: () => rpc.call<ProbeResult>('providers.probe', { providerId }),
  })
  const verdict: 'checking' | 'ok' | 'bad' | 'unknown' = probe.isPending
    ? 'checking'
    : probe.data?.ok
      ? 'ok'
      : probe.data
        ? 'bad'
        : 'unknown'
  const detail = probe.data?.error ?? probe.error?.message ?? ''
  const modelCount = probe.data?.models.length ?? 0

  return (
    <div className="setup__ready" data-testid="setup-ready" data-verdict={verdict}>
      <span className="setup__ready-mark" aria-hidden data-tone={verdict === 'bad' ? 'warn' : 'ok'}>
        {verdict === 'bad' ? (
          <AlertTriangle className="size-7" strokeWidth={2.25} />
        ) : (
          <Check className="size-7" strokeWidth={2.5} />
        )}
      </span>
      <h1 className="setup__title">{t('setup.ready.title')}</h1>
      <p className="setup__blurb">
        {provider ? (
          <>
            <b>{provider}</b> {t('setup.ready.blurb')}
          </>
        ) : (
          t('setup.ready.blurb.none')
        )}
      </p>
      <p className="setup__verdict" data-verdict={verdict} data-testid="setup-key-verdict">
        {verdict === 'checking' ? (
          <>
            <LoaderCircle className="setup__spin size-3.5" aria-hidden />
            {t('setup.ready.checking')}
          </>
        ) : verdict === 'ok' ? (
          <>
            <Check className="size-3.5" aria-hidden />
            {t('setup.ready.keyOk')}
            {modelCount ? ` ${modelCount} ${t('setup.ready.models')}.` : ''}
          </>
        ) : verdict === 'bad' ? (
          <>
            <AlertTriangle className="size-3.5" aria-hidden />
            {t('setup.ready.keyBad')} <code>{detail}</code>
          </>
        ) : (
          t('setup.ready.keyUnknown')
        )}
      </p>
      <div className="setup__actions setup__actions--center">
        {verdict === 'bad' ? (
          <>
            <Button onClick={onEditKey}>{t('setup.ready.editKey')}</Button>
            <Button variant="primary" onClick={onDone}>
              {t('setup.ready.continueAnyway')}
            </Button>
          </>
        ) : (
          <Button
            variant="primary"
            className="setup__primary"
            disabled={verdict === 'checking'}
            onClick={onDone}
          >
            {t('setup.ready.open')}
          </Button>
        )}
      </div>
    </div>
  )
}
