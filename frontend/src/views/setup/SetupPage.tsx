import './setup.css'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowRightIcon,
  BotIcon,
  BoxesIcon,
  CircleAlertIcon,
  ClipboardCheckIcon,
  RadioTowerIcon,
  RouteIcon,
  type LucideIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { Button } from '@/components/ui/button'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { ProviderSection } from './ProviderSection'
import { RouterSection } from './RouterSection'
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

const STEP_ICONS: Record<StepId, LucideIcon> = {
  provider: BotIcon,
  router: RouteIcon,
  channels: RadioTowerIcon,
  extras: BoxesIcon,
  finish: ClipboardCheckIcon,
}

interface SetupPageProps {
  embedded?: boolean
  /**
   * `undefined` keeps the standalone compatibility loader used by focused
   * tests and direct embeds. `null` means an owning Settings workspace is
   * still loading its atomic snapshot.
   */
  externalSnapshot?: SettingsSnapshot | null
  onSnapshotReload?: () => Promise<SettingsSnapshot | undefined>
}

type GuidedResetTarget =
  | 'provider'
  | 'router'
  | 'search'
  | 'memoryEmbedding'
  | 'memorySettings'
  | 'image'
  | 'audio'
  | 'finish'

const INITIAL_RESET_VERSIONS: Record<GuidedResetTarget, number> = {
  provider: 0,
  router: 0,
  search: 0,
  memoryEmbedding: 0,
  memorySettings: 0,
  image: 0,
  audio: 0,
  finish: 0,
}

const GUIDED_RESET_TARGETS = Object.keys(INITIAL_RESET_VERSIONS) as GuidedResetTarget[]

function initialTargetFlags(): Record<GuidedResetTarget, boolean> {
  return Object.fromEntries(GUIDED_RESET_TARGETS.map((target) => [target, false])) as Record<
    GuidedResetTarget,
    boolean
  >
}

function initialTargetRevisions(
  revision?: string | null,
): Record<GuidedResetTarget, string | undefined> {
  return Object.fromEntries(
    GUIDED_RESET_TARGETS.map((target) => [target, revision ?? undefined]),
  ) as Record<GuidedResetTarget, string | undefined>
}

export function SetupPage({
  embedded = false,
  externalSnapshot,
  onSnapshotReload,
}: SetupPageProps = {}) {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const usesExternalSnapshot = externalSnapshot !== undefined

  useEffect(() => {
    if (!embedded) document.title = 'Setup - AgentOS Control'
  }, [embedded])

  // setup.js:62-79 — compatibility loader for catalog + status + config.
  const catalogQuery = useQuery<Catalog>({
    queryKey: ['setup', 'catalog'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await rpc.call<Catalog>('onboarding.catalog')) ?? {}
    },
    enabled: !usesExternalSnapshot,
    refetchOnWindowFocus: false,
  })
  const statusQuery = useQuery<OnboardingStatus>({
    queryKey: ['setup', 'status'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await rpc.call<OnboardingStatus>('onboarding.status')) ?? {}
    },
    enabled: !usesExternalSnapshot,
    refetchOnWindowFocus: false,
  })
  const configQuery = useQuery<SetupConfig>({
    queryKey: ['setup', 'config'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await rpc.call<SetupConfig>('config.get')) ?? {}
    },
    enabled: !usesExternalSnapshot,
    refetchOnWindowFocus: false,
  })

  const [step, setStep] = useState<StepId | null>(null)
  const [resetVersions, setResetVersions] = useState(INITIAL_RESET_VERSIONS)
  const [targetDirty, setTargetDirty] = useState(initialTargetFlags)
  const currentExternalRevision = externalSnapshot?.revision ?? undefined
  const [targetRevisions, setTargetRevisions] = useState(() =>
    initialTargetRevisions(currentExternalRevision),
  )
  const [lastExternalRevision, setLastExternalRevision] = useState(currentExternalRevision)

  if (currentExternalRevision && currentExternalRevision !== lastExternalRevision) {
    setLastExternalRevision(currentExternalRevision)
    // Pristine forms can safely adopt the coherent snapshot immediately. Dirty
    // forms retain both their draft and the revision they were based on; their
    // save control is blocked below until the operator explicitly discards it.
    setResetVersions(
      (versions) =>
        Object.fromEntries(
          GUIDED_RESET_TARGETS.map((target) => [
            target,
            targetDirty[target] ? versions[target] : versions[target] + 1,
          ]),
        ) as Record<GuidedResetTarget, number>,
    )
    setTargetRevisions(
      (revisions) =>
        Object.fromEntries(
          GUIDED_RESET_TARGETS.map((target) => [
            target,
            targetDirty[target] ? revisions[target] : currentExternalRevision,
          ]),
        ) as Record<GuidedResetTarget, string | undefined>,
    )
  }

  const markTargetDirty = (target: GuidedResetTarget) => {
    setTargetDirty((dirty) => (dirty[target] ? dirty : { ...dirty, [target]: true }))
  }

  const resetSavedTarget = (target: GuidedResetTarget, revision?: string) => {
    setResetVersions((versions) => ({ ...versions, [target]: versions[target] + 1 }))
    setTargetDirty((dirty) => ({ ...dirty, [target]: false }))
    if (revision) {
      setTargetRevisions((revisions) => ({ ...revisions, [target]: revision }))
    }
  }

  const adoptTargetRevision = (target: GuidedResetTarget, revision?: string) => {
    if (!revision) return
    setTargetRevisions((revisions) => ({ ...revisions, [target]: revision }))
  }

  const catalog = externalSnapshot?.catalog ?? catalogQuery.data ?? {}
  const status = useMemo(
    () => externalSnapshot?.status ?? statusQuery.data ?? {},
    [externalSnapshot?.status, statusQuery.data],
  )
  const config = useMemo(
    () => externalSnapshot?.config ?? configQuery.data ?? {},
    [externalSnapshot?.config, configQuery.data],
  )
  const loaded = usesExternalSnapshot
    ? externalSnapshot !== null
    : catalogQuery.isSuccess && statusQuery.isSuccess && configQuery.isSuccess
  const loadFailed = usesExternalSnapshot
    ? false
    : catalogQuery.isError || statusQuery.isError || configQuery.isError

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
  const conflictedTargets = currentExternalRevision
    ? GUIDED_RESET_TARGETS.filter(
        (target) => targetDirty[target] && targetRevisions[target] !== currentExternalRevision,
      )
    : []
  const targetConflicted = (target: GuidedResetTarget) => conflictedTargets.includes(target)
  const writeBlocked = externalSnapshot?.writeBlocked === true

  const discardConflictedDrafts = () => {
    if (!currentExternalRevision || conflictedTargets.length === 0) return
    setResetVersions((versions) => {
      const next = { ...versions }
      conflictedTargets.forEach((target) => {
        next[target] += 1
      })
      return next
    })
    setTargetDirty((dirty) => {
      const next = { ...dirty }
      conflictedTargets.forEach((target) => {
        next[target] = false
      })
      return next
    })
    setTargetRevisions((revisions) => {
      const next = { ...revisions }
      conflictedTargets.forEach((target) => {
        next[target] = currentExternalRevision
      })
      return next
    })
    if (conflictedTargets.includes('provider')) setDraftProvider(null)
  }

  // Reload all setup reads (setup.js:1316,1751,1848,…).
  const reload = async (): Promise<SettingsSnapshot | undefined> => {
    if (usesExternalSnapshot && onSnapshotReload) {
      return await onSnapshotReload()
    }
    await queryClient.invalidateQueries({ queryKey: ['setup'] })
    return undefined
  }

  const reloadAfterSave = async (toastId: string): Promise<SettingsSnapshot | undefined> => {
    try {
      return await reload()
    } catch {
      toast.warning('Saved, but the latest agent state could not be refreshed.', {
        id: `${toastId}-refresh`,
      })
      return undefined
    }
  }

  // Guided and Advanced edit the same persisted document. When Settings owns
  // an atomic snapshot, carry its revision into every guided write so a stale
  // browser tab cannot silently overwrite a newer configuration.
  const withExpectedRevision = <T extends Record<string, unknown>>(
    target: GuidedResetTarget,
    params: T,
  ) => {
    if (writeBlocked) {
      throw new Error('config file changed outside AgentOS; restart or reload the gateway first')
    }
    return targetRevisions[target]
      ? { ...params, expectedRevision: targetRevisions[target] }
      : params
  }

  // ── mutations (one per onboarding.*.configure / config.patch) ─────────────

  const providerMutation = useMutation({
    mutationFn: (vars: { providerId: string; params: Record<string, unknown> }) =>
      rpc.call(
        'onboarding.provider.configure',
        withExpectedRevision('provider', { providerId: vars.providerId, ...vars.params }),
      ),
    onSuccess: async () => {
      resetSavedTarget('provider')
      // setup.js:1751-1761 — reload, then re-check the env-missing guard on the
      // FRESH status (the refetch result, not the stale cached data/config).
      let freshStatus: OnboardingStatus = {}
      let freshConfig: SetupConfig = {}
      let freshRevision: string | undefined
      try {
        if (usesExternalSnapshot && onSnapshotReload) {
          const fresh = await onSnapshotReload()
          freshStatus = fresh?.status ?? {}
          freshConfig = fresh?.config ?? {}
          freshRevision = fresh?.revision ?? undefined
        } else {
          const [statusResult, configResult] = await Promise.all([
            statusQuery.refetch(),
            configQuery.refetch(),
          ])
          void queryClient.invalidateQueries({ queryKey: ['setup'] })
          freshStatus = statusResult.data ?? {}
          freshConfig = configResult.data ?? {}
        }
      } catch {
        toast.warning('Provider saved, but the latest agent state could not be refreshed.', {
          id: 'setup-provider-refresh',
        })
        return
      }
      // Re-mount only the form that was just committed. This adopts the
      // canonical response and removes any one-time pasted credential without
      // disturbing drafts in the other persistently mounted guided steps.
      adoptTargetRevision('provider', freshRevision)
      const s = freshStatus
      if (providerEnvMissing(s)) {
        toast.error(`${providerEnvKey(freshConfig)} is not visible to this gateway process.`, {
          id: 'setup-provider',
        })
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
      rpc.call(
        'onboarding.router.configure',
        withExpectedRevision('router', params as unknown as Record<string, unknown>),
      ),
    onSuccess: async () => {
      toast.info('Router saved.', { id: 'setup-router' })
      resetSavedTarget('router')
      const fresh = await reloadAfterSave('setup-router')
      adoptTargetRevision('router', fresh?.revision ?? undefined)
      setStep('extras')
    },
    onError: (err) => saveError('setup-router', err),
  })

  const searchMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call('onboarding.search.configure', withExpectedRevision('search', params)),
    onSuccess: async () => {
      toast.info('Search saved.', { id: 'setup-search' })
      resetSavedTarget('search')
      const fresh = await reloadAfterSave('setup-search')
      adoptTargetRevision('search', fresh?.revision ?? undefined)
    },
    onError: (err) => saveError('setup-search', err),
  })

  interface ConfigureResult {
    entry?: { api_key_env?: string; api_key?: string; api_key_source?: string }
    restartRequired?: boolean
  }

  const memoryMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>(
        'onboarding.memory_embedding.configure',
        withExpectedRevision('memoryEmbedding', params),
      ),
    onSuccess: async (res) => {
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
      resetSavedTarget('memoryEmbedding')
      const fresh = await reloadAfterSave('setup-memory')
      adoptTargetRevision('memoryEmbedding', fresh?.revision ?? undefined)
    },
    onError: (err) => saveError('setup-memory', err),
  })

  const memorySettingsMutation = useMutation({
    mutationFn: (patches: Record<string, unknown>) =>
      rpc.call<ConfigureResult>(
        'config.patch',
        withExpectedRevision('memorySettings', { patches }),
      ),
    onSuccess: async (res) => {
      toast.info(
        res?.restartRequired
          ? 'Memory settings saved. Restart required.'
          : 'Memory settings saved.',
        { id: 'setup-memory-settings' },
      )
      resetSavedTarget('memorySettings')
      const fresh = await reloadAfterSave('setup-memory-settings')
      adoptTargetRevision('memorySettings', fresh?.revision ?? undefined)
    },
    onError: (err) => saveError('setup-memory-settings', err),
  })

  const imageMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>(
        'onboarding.imageGeneration.configure',
        withExpectedRevision('image', params),
      ),
    onSuccess: async (res) => {
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
      resetSavedTarget('image')
      const fresh = await reloadAfterSave('setup-image')
      adoptTargetRevision('image', fresh?.revision ?? undefined)
    },
    onError: (err) => saveError('setup-image', err),
  })

  const audioMutation = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>(
        'onboarding.audio.configure',
        withExpectedRevision('audio', params),
      ),
    onSuccess: async (res) => {
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
      resetSavedTarget('audio')
      const fresh = await reloadAfterSave('setup-audio')
      adoptTargetRevision('audio', fresh?.revision ?? undefined)
    },
    onError: (err) => saveError('setup-audio', err),
  })

  const updatesMutation = useMutation({
    mutationFn: (notify: boolean) =>
      rpc.call(
        'config.patch',
        withExpectedRevision('finish', { patches: { 'updates.notify': notify } }),
      ),
    onSuccess: async () => {
      toast.info('Update preference saved.', { id: 'setup-updates' })
      resetSavedTarget('finish')
      const fresh = await reloadAfterSave('setup-updates')
      adoptTargetRevision('finish', fresh?.revision ?? undefined)
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
    <div className={`setup-stage${embedded ? ' setup-stage--embedded' : ''}`}>
      {!embedded ? (
        <header className="setup-stage__header">
          <div className="setup-stage__title-block">
            <span className="t-label">Control · Setup</span>
            <h1 className="t-display">Setup</h1>
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
      ) : null}

      {reasons.length || conflictedTargets.length > 0 ? (
        <div className="setup-notices">
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
                    onClick={() =>
                      reason.step === 'channels'
                        ? navigate('/channels?view=setup')
                        : setStep(reason.step)
                    }
                  >
                    <CircleAlertIcon className="setup-reasons__icon" aria-hidden="true" />
                    <span className="setup-reasons__text">{reason.text}</span>
                    <span className="setup-reasons__cta">
                      {reason.tier === 'blocking' ? 'Continue' : 'Review'}
                      <ArrowRightIcon aria-hidden="true" />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}

          {conflictedTargets.length > 0 ? (
            <div className="setup-warning panel tone-warn tone-rail" role="alert">
              <span>
                The configuration changed while{' '}
                {conflictedTargets.length === 1 ? 'a guided draft was' : 'guided drafts were'} open.
                Review the latest state, then discard the stale{' '}
                {conflictedTargets.length === 1 ? 'draft' : 'drafts'} before saving.
              </span>
              <Button type="button" size="sm" variant="outline" onClick={discardConflictedDrafts}>
                Discard stale drafts
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      <nav className="setup-stepper" aria-label="Setup steps">
        {STEPS.map((s) => {
          const st = stepStatus(s.id, status, effectiveProviderId)
          const StepIcon = STEP_ICONS[s.id]
          return (
            <button
              key={s.id}
              type="button"
              className={`setup-stepper__item${s.id === currentStep ? ' is-active' : ''}`}
              aria-label={`${s.label}: ${st.label}`}
              aria-current={s.id === currentStep ? 'step' : undefined}
              onClick={() => setStep(s.id)}
            >
              <span className="setup-stepper__icon" aria-hidden="true">
                <StepIcon />
              </span>
              <span className="setup-stepper__copy">
                <span className="setup-stepper__label">{s.label}</span>
                <small className={`setup-stepper__state ${STEP_TONE[st.tone]}`}>{st.label}</small>
              </span>
            </button>
          )
        })}
      </nav>

      <div className="setup-body">
        {embedded || currentStep === 'provider' ? (
          <div
            className="setup-step-panel"
            hidden={currentStep !== 'provider'}
            onChangeCapture={() => markTargetDirty('provider')}
          >
            <ProviderSection
              key={`provider:${resetVersions.provider}`}
              catalog={catalog}
              status={status}
              config={config}
              saving={providerMutation.isPending || targetConflicted('provider') || writeBlocked}
              onSave={(providerId, params) => providerMutation.mutate({ providerId, params })}
              onNext={() => setStep('router')}
              onProviderChange={setDraftProvider}
            />
          </div>
        ) : null}
        {embedded || currentStep === 'router' ? (
          <div
            className="setup-step-panel"
            hidden={currentStep !== 'router'}
            onChangeCapture={() => markTargetDirty('router')}
          >
            <RouterSection
              key={`router:${resetVersions.router}`}
              catalog={catalog}
              status={status}
              config={config}
              draftProvider={draftProvider ?? ''}
              saving={routerMutation.isPending || targetConflicted('router') || writeBlocked}
              onSave={(params) => routerMutation.mutate(params)}
              onBack={() => setStep('provider')}
              onNext={() => setStep('extras')}
            />
          </div>
        ) : null}
        {embedded || currentStep === 'extras' ? (
          <div className="setup-step-panel" hidden={currentStep !== 'extras'}>
            <ExtrasSection
              catalog={catalog}
              status={status}
              config={config}
              saving={
                writeBlocked ||
                searchMutation.isPending ||
                memoryMutation.isPending ||
                memorySettingsMutation.isPending ||
                imageMutation.isPending ||
                audioMutation.isPending
              }
              resetVersions={{
                search: resetVersions.search,
                memoryEmbedding: resetVersions.memoryEmbedding,
                memorySettings: resetVersions.memorySettings,
                image: resetVersions.image,
                audio: resetVersions.audio,
              }}
              conflicts={{
                search: targetConflicted('search'),
                memoryEmbedding: targetConflicted('memoryEmbedding'),
                memorySettings: targetConflicted('memorySettings'),
                image: targetConflicted('image'),
                audio: targetConflicted('audio'),
              }}
              onDirtyChange={markTargetDirty}
              onSaveSearch={(params) => searchMutation.mutate(params)}
              onSaveMemory={(params) => memoryMutation.mutate(params)}
              onSaveMemorySettings={(patches) => memorySettingsMutation.mutate(patches)}
              onSaveImage={(params) => imageMutation.mutate(params)}
              onSaveAudio={(params) => audioMutation.mutate(params)}
              onBack={() => setStep('router')}
              onNext={() => setStep('finish')}
            />
          </div>
        ) : null}
        {embedded || currentStep === 'finish' ? (
          <div
            className="setup-step-panel"
            hidden={currentStep !== 'finish'}
            onChangeCapture={() => markTargetDirty('finish')}
          >
            <FinishSection
              key={`finish:${resetVersions.finish}`}
              status={status}
              config={config}
              saving={updatesMutation.isPending || targetConflicted('finish') || writeBlocked}
              onBack={() => setStep('extras')}
              onReload={reload}
              onExit={() => navigate('/overview')}
              onGoStep={(s) => (s === 'channels' ? navigate('/channels?view=setup') : setStep(s))}
              onSaveUpdatesNotify={(notify) => updatesMutation.mutate(notify)}
            />
          </div>
        ) : null}
      </div>
    </div>
  )
}
