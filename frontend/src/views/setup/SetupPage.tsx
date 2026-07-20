import './setup.css'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AsciiField } from '@/components/AsciiField'
import { useRpc } from '@/app/providers'
import { ProviderSection } from './ProviderSection'
import { RouterSection } from './RouterSection'
import { ChannelsSection, type ChannelsRuntimeStatus } from './ChannelsSection'
import { ExtrasSection } from './ExtrasSection'
import { FinishSection } from './FinishSection'
import {
  effectiveProvider as effectiveProviderFn,
  envReferenceSaveAdvisory,
  initialStepFromStatus,
  onboardingReasons,
  providerEnvKey,
  providerEnvMissing,
  setupHeadline,
  stepStatus,
  STEPS,
  type Catalog,
  type OnboardingStatus,
  type RouterConfigureParams,
  type SetupConfig,
  type StepId,
} from './logic'

// setup.js:2002-2007 — the 5s channel-status poll cadence.
const CHANNEL_POLL_MS = 5000

const HEADLINE_TONE: Record<string, string> = {
  'is-warn': 'tone-warn',
  'is-optional': 'tone-info',
  'is-ok': 'tone-ok',
}
const STEP_TONE: Record<string, string> = {
  'is-ok': 'tone-ok',
  'is-warn': 'tone-warn',
  'is-muted': 'tone-dim',
}

export function SetupPage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  useEffect(() => {
    document.title = 'Setup - AgentOS Control'
  }, [])

  // setup.js:62-79 — parallel catalog + status + config + channels + memory doctor.
  const catalogQuery = useQuery<Catalog>({
    queryKey: ['setup', 'catalog'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await rpc.call<Catalog>('onboarding.catalog')) ?? {}
    },
    refetchOnWindowFocus: false,
  })
  const statusQuery = useQuery<OnboardingStatus>({
    queryKey: ['setup', 'status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await rpc.call<OnboardingStatus>('onboarding.status')) ?? {}
    },
    refetchOnWindowFocus: false,
  })
  const configQuery = useQuery<SetupConfig>({
    queryKey: ['setup', 'config'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await rpc.call<SetupConfig>('config.get')) ?? {}
    },
    refetchOnWindowFocus: false,
  })

  // Channel dirty state pauses the poll (setup.js:2003-2004).
  const [channelDirty, setChannelDirty] = useState(false)
  const [step, setStep] = useState<StepId | null>(null)

  const channelStatusQuery = useQuery<ChannelsRuntimeStatus>({
    queryKey: ['setup', 'channels'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return await rpc
        .call<ChannelsRuntimeStatus>('channels.status')
        .catch(() => ({ channels: [] }) as ChannelsRuntimeStatus)
    },
    // setup.js:2002-2008 — poll only while on the channels step and not dirty.
    refetchInterval: step === 'channels' && !channelDirty ? CHANNEL_POLL_MS : false,
    refetchOnWindowFocus: false,
  })

  const catalog = catalogQuery.data ?? {}
  const status = useMemo(() => statusQuery.data ?? {}, [statusQuery.data])
  const config = useMemo(() => configQuery.data ?? {}, [configQuery.data])
  const channelStatus = channelStatusQuery.data ?? { channels: [] }

  const loaded = catalogQuery.isSuccess && statusQuery.isSuccess && configQuery.isSuccess
  const loadFailed = catalogQuery.isError || statusQuery.isError || configQuery.isError

  // setup.js:280-300 — auto-select the initial step once status is known.
  const [autoSelected, setAutoSelected] = useState(false)
  if (loaded && !autoSelected) {
    setAutoSelected(true)
    setStep(initialStepFromStatus(status))
  }
  const currentStep: StepId = step ?? 'provider'

  // The provider drafted in the Provider step, lifted so the Router preview and
  // the stepper chip see it before Save (legacy _draftProvider, setup.js:430-435).
  // null → no draft yet; fall back to the configured/effective provider.
  const [draftProvider, setDraftProvider] = useState<string | null>(null)
  const effectiveProviderId = effectiveProviderFn(status, config, draftProvider ?? '')
  const reasons = useMemo(() => onboardingReasons(status, config), [status, config])
  const headline = setupHeadline(reasons)

  // Reload all setup reads (setup.js:1316,1751,1848,…).
  const reload = () => {
    void queryClient.invalidateQueries({ queryKey: ['setup'] })
  }

  // ── mutations (one per onboarding.*.configure / config.patch) ─────────────

  const providerMutation = useMutation({
    mutationFn: (vars: { providerId: string; params: Record<string, unknown> }) =>
      rpc.call('onboarding.provider.configure', { providerId: vars.providerId, ...vars.params }),
    onSuccess: async () => {
      // setup.js:1751-1761 — reload, then re-check the env-missing guard on the
      // FRESH status (the refetch result, not the stale cached data/config).
      const [freshStatus, freshConfig] = await Promise.all([
        statusQuery.refetch(),
        configQuery.refetch(),
      ])
      void queryClient.invalidateQueries({ queryKey: ['setup'] })
      const s = freshStatus.data ?? {}
      if (providerEnvMissing(s)) {
        toast.error(
          `${providerEnvKey(freshConfig.data ?? {})} is not visible to this gateway process.`,
          { id: 'setup-provider' },
        )
        setStep('provider')
        return
      }
      toast.info('Provider saved.', { id: 'setup-provider' })
      setStep('router')
    },
    onError: (err) => saveError('setup-provider', err),
  })

  const routerMutation = useMutation({
    mutationFn: (params: RouterConfigureParams) =>
      rpc.call('onboarding.router.configure', params as unknown as Record<string, unknown>),
    onSuccess: () => {
      toast.info('Router saved.', { id: 'setup-router' })
      reload()
      setStep('channels')
    },
    onError: (err) => saveError('setup-router', err),
  })

  const channelMutation = useMutation({
    // setup.js:1865-1866 — probe THEN upsert.
    mutationFn: async (entry: Record<string, unknown>) => {
      await rpc.call('onboarding.channel.probe', { entry })
      await rpc.call('onboarding.channel.upsert', { entry })
    },
    onSuccess: () => {
      toast.info('Channel saved. Restart required.', { id: 'setup-channel' })
      setChannelDirty(false)
      void channelStatusQuery.refetch()
    },
    onError: (err) => saveError('setup-channel', err),
  })

  const searchMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call('onboarding.search.configure', params),
    onSuccess: () => {
      toast.info('Search saved.', { id: 'setup-search' })
      reload()
    },
    onError: (err) => saveError('setup-search', err),
  })

  interface ConfigureResult {
    entry?: { api_key_env?: string; api_key?: string; api_key_source?: string }
    restartRequired?: boolean
  }

  const memoryMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>('onboarding.memory_embedding.configure', params),
    onSuccess: (res) => {
      const remote = (res?.entry as { remote?: Record<string, string> })?.remote || {}
      const advisory = envReferenceSaveAdvisory({
        surface: 'Memory embedding',
        envKey: remote.api_key_env,
        hasInlineKey: remote.api_key,
        restartRequired: res?.restartRequired,
      })
      if (advisory.kind === 'warn') toast.warning(advisory.message, { id: 'setup-memory' })
      else if (advisory.kind === 'info') toast.info(advisory.message, { id: 'setup-memory' })
      else toast.info('Memory embedding saved. Restart required.', { id: 'setup-memory' })
      reload()
    },
    onError: (err) => saveError('setup-memory', err),
  })

  const memorySettingsMutation = useMutation({
    mutationFn: (patches: Record<string, unknown>) =>
      rpc.call<ConfigureResult>('config.patch', { patches }),
    onSuccess: (res) => {
      toast.info(
        res?.restartRequired
          ? 'Memory settings saved. Restart required.'
          : 'Memory settings saved.',
        { id: 'setup-memory-settings' },
      )
      reload()
    },
    onError: (err) => saveError('setup-memory-settings', err),
  })

  const imageMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>('onboarding.imageGeneration.configure', params),
    onSuccess: (res) => {
      const entry = res?.entry || {}
      const advisory = envReferenceSaveAdvisory({
        surface: 'Image generation',
        envKey: entry.api_key_env,
        keySource: entry.api_key_source,
        hasInlineKey: entry.api_key,
        restartRequired: res?.restartRequired,
      })
      if (advisory.kind === 'warn') toast.warning(advisory.message, { id: 'setup-image' })
      else if (advisory.kind === 'info') toast.info(advisory.message, { id: 'setup-image' })
      else toast.info('Image generation saved.', { id: 'setup-image' })
      reload()
    },
    onError: (err) => saveError('setup-image', err),
  })

  const audioMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>('onboarding.audio.configure', params),
    onSuccess: (res) => {
      const entry = res?.entry || {}
      const advisory = envReferenceSaveAdvisory({
        surface: 'Voice audio',
        envKey: entry.api_key_env,
        keySource: entry.api_key_source,
        hasInlineKey: entry.api_key,
        restartRequired: res?.restartRequired,
      })
      if (advisory.kind === 'warn') toast.warning(advisory.message, { id: 'setup-audio' })
      else if (advisory.kind === 'info') toast.info(advisory.message, { id: 'setup-audio' })
      else toast.info('Voice audio saved.', { id: 'setup-audio' })
      reload()
    },
    onError: (err) => saveError('setup-audio', err),
  })

  const updatesMutation = useMutation({
    mutationFn: (notify: boolean) =>
      rpc.call('config.patch', { patches: { 'updates.notify': notify } }),
    onSuccess: () => {
      toast.info('Update preference saved.', { id: 'setup-updates' })
      reload()
    },
    onError: (err) => saveError('setup-updates', err),
  })

  function saveError(id: string, err: unknown) {
    const message = err instanceof Error ? err.message : String(err)
    toast.error('Save failed: ' + message, { id })
  }

  if (loadFailed) {
    const err = catalogQuery.error || statusQuery.error || configQuery.error
    const message = err instanceof Error ? err.message : String(err)
    return (
      <div className="setup-stage">
        <div className="setup-error panel tone-danger tone-rail">
          Failed to load setup catalog: {message}
        </div>
      </div>
    )
  }

  if (!loaded) {
    return (
      <div className="setup-stage">
        <p className="setup-muted">Loading setup…</p>
      </div>
    )
  }

  return (
    <div className="setup-stage">
      <header className="setup-stage__header">
        <AsciiField />
        <div className="setup-stage__title-block">
          <span className="t-label">Control · Setup</span>
          <h2 className="t-display">Setup</h2>
          <p className="setup-stage__subtitle">{headline.title}</p>
        </div>
        <div className="setup-stage__aside">
          <button
            type="button"
            className="setup-exit"
            aria-label="Exit setup and return to Overview"
            onClick={() => navigate('/overview')}
          >
            <span aria-hidden="true">←</span>
            <span>Exit setup</span>
          </button>
          <div className={`setup-status ${HEADLINE_TONE[headline.tone]}`}>{headline.chip}</div>
        </div>
      </header>

      {reasons.length ? (
        <ul
          className="setup-reasons"
          aria-label={
            headline.tone === 'is-warn' ? 'Setup actions needed' : 'Optional improvements'
          }
        >
          {reasons.map((reason) => (
            <li
              className={`setup-reasons__item ${reason.tier === 'blocking' ? 'tone-warn' : 'tone-info'}`}
              key={reason.text}
            >
              <button
                type="button"
                className="setup-reasons__action"
                onClick={() => setStep(reason.step)}
              >
                <span>{reason.text}</span>
                <span aria-hidden="true">{reason.tier === 'blocking' ? 'Fix →' : 'Review →'}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <nav className="setup-stepper" aria-label="Setup steps">
        {STEPS.map((s, idx) => {
          const st = stepStatus(s.id, status, effectiveProviderId)
          return (
            <button
              key={s.id}
              type="button"
              className={`setup-stepper__item${s.id === currentStep ? ' is-active' : ''}`}
              aria-label={`${s.label}: ${st.label}`}
              aria-current={s.id === currentStep ? 'step' : undefined}
              onClick={() => setStep(s.id)}
            >
              <span className="setup-stepper__num">{idx + 1}</span>
              <span className="setup-stepper__label">{s.label}</span>
              <small className={`setup-stepper__state ${STEP_TONE[st.tone]}`}>{st.label}</small>
            </button>
          )
        })}
      </nav>

      <div className="setup-body">
        {currentStep === 'provider' ? (
          <ProviderSection
            catalog={catalog}
            status={status}
            config={config}
            saving={providerMutation.isPending}
            onSave={(providerId, params) => providerMutation.mutate({ providerId, params })}
            onNext={() => setStep('router')}
            onProviderChange={setDraftProvider}
          />
        ) : null}
        {currentStep === 'router' ? (
          <RouterSection
            catalog={catalog}
            status={status}
            config={config}
            draftProvider={draftProvider ?? ''}
            saving={routerMutation.isPending}
            onSave={(params) => routerMutation.mutate(params)}
            onBack={() => setStep('provider')}
            onNext={() => setStep('channels')}
          />
        ) : null}
        {currentStep === 'channels' ? (
          <ChannelsSection
            catalog={catalog}
            channelStatus={channelStatus}
            saving={channelMutation.isPending}
            onSave={(entry) => channelMutation.mutate(entry)}
            onBack={() => setStep('router')}
            onNext={() => setStep('extras')}
            onDirtyChange={setChannelDirty}
            onValidationError={(msg) => toast.error(msg, { id: 'setup-channel' })}
          />
        ) : null}
        {currentStep === 'extras' ? (
          <ExtrasSection
            catalog={catalog}
            status={status}
            config={config}
            saving={
              searchMutation.isPending ||
              memoryMutation.isPending ||
              memorySettingsMutation.isPending ||
              imageMutation.isPending ||
              audioMutation.isPending
            }
            onSaveSearch={(params) => searchMutation.mutate(params)}
            onSaveMemory={(params) => memoryMutation.mutate(params)}
            onSaveMemorySettings={(patches) => memorySettingsMutation.mutate(patches)}
            onSaveImage={(params) => imageMutation.mutate(params)}
            onSaveAudio={(params) => audioMutation.mutate(params)}
            onBack={() => setStep('channels')}
            onNext={() => setStep('finish')}
          />
        ) : null}
        {currentStep === 'finish' ? (
          <FinishSection
            status={status}
            config={config}
            saving={updatesMutation.isPending}
            onBack={() => setStep('extras')}
            onReload={reload}
            onExit={() => navigate('/overview')}
            onGoStep={(s) => setStep(s)}
            onSaveUpdatesNotify={(notify) => updatesMutation.mutate(notify)}
          />
        ) : null}
      </div>
    </div>
  )
}
