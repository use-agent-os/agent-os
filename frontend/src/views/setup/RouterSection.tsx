// Router section (setup.js:550-635,1790-1855). Mode (Pilot / LLM judge / Off),
// default text model tier, judge model, pilot safety-net threshold, and the
// editable tier table. Save via onboarding.router.configure, gated on the
// provider being saved (effective === configured).
import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { PanelHead, SetupCheckbox, SetupSelect } from './parts'
import {
  buildRouterConfigureParams,
  configuredProvider as configuredProviderFn,
  effectiveProvider as effectiveProviderFn,
  isVisibleTier,
  mergeTiers,
  resolveJudgeModelParam,
  routerMode as routerModeFn,
  tierLabel,
  TEXT_TIERS,
  type Catalog,
  type OnboardingStatus,
  type RouterConfigureParams,
  type RouterMode,
  type SetupConfig,
  type TierSpec,
} from './logic'

const THINKING_LEVELS = ['', 'off', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']

interface TierRowState {
  provider: string
  model: string
  thinkingLevel: string
  supportsImage: boolean
}

export function RouterSection({
  catalog,
  status,
  config,
  draftProvider = '',
  onSave,
  onBack,
  onNext,
  saving,
}: {
  catalog: Catalog
  status: OnboardingStatus
  config: SetupConfig
  // The provider drafted in the Provider step (not yet saved). Preview/table
  // render on the effective provider — draft OR configured (setup.js:552-556).
  draftProvider?: string
  onSave: (params: RouterConfigureParams) => void
  onBack: () => void
  onNext: () => void
  saving: boolean
}) {
  const router = config.agentos_router || {}
  const provider = effectiveProviderFn(status, config, draftProvider)
  const configured = configuredProviderFn(status, config)
  const canSave = Boolean(provider && provider === configured)

  const routerCatalog = catalog.routerProfiles || {}
  const profiles = Array.isArray(routerCatalog.profiles) ? routerCatalog.profiles : []
  const profile = provider ? profiles.find((p) => p?.providerId === provider) : undefined
  const tiers = useMemo(
    () => (provider ? mergeTiers(profile?.tiers, router.tiers) : {}),
    [provider, profile?.tiers, router.tiers],
  )
  const defaultTierInitial = router.default_tier || routerCatalog.defaultTier || 'c1'

  const [mode, setMode] = useState<RouterMode>(routerModeFn(router))
  const [defaultTier, setDefaultTier] = useState(defaultTierInitial)

  const pilotThresholdInitial =
    router.pilot?.safety_net_threshold != null ? String(router.pilot.safety_net_threshold) : '0.5'
  const [pilotThreshold, setPilotThreshold] = useState(pilotThresholdInitial)

  // Judge model catalog: AUTO is judge_model === null → the empty option.
  const judgeCatalog = routerCatalog.judge || {}
  const judgeProfiles =
    judgeCatalog.profiles &&
    typeof judgeCatalog.profiles === 'object' &&
    !Array.isArray(judgeCatalog.profiles)
      ? judgeCatalog.profiles
      : {}
  const judgeProfile = provider ? judgeProfiles[provider] || {} : {}
  const judgeAutoModel = typeof judgeProfile.autoModel === 'string' ? judgeProfile.autoModel : null
  const judgeModels = Array.isArray(judgeProfile.models)
    ? judgeProfile.models.filter((model): model is string => typeof model === 'string')
    : []
  const judgeLoaded = router.judge_model || ''
  const judgeIsLocal = Boolean(router.judge_base_url)
  const [judge, setJudge] = useState(judgeLoaded)
  const judgeAutoLabel = judgeAutoModel
    ? `Auto (recommended) - ${judgeAutoModel}`
    : 'Auto (recommended)'

  // Editable tier rows (only text tiers + image_model).
  const visibleTiers = Object.entries(tiers).filter(([name]) => isVisibleTier(name))
  const [rowKey, setRowKey] = useState(provider)
  const [rows, setRows] = useState<Record<string, TierRowState>>(() => seedRows(visibleTiers))
  if (rowKey !== provider) {
    setRowKey(provider)
    setRows(seedRows(visibleTiers))
  }

  const rowFor = (name: string, tier: TierSpec): TierRowState => rows[name] ?? tierRowState(tier)

  const setRow = (name: string, tier: TierSpec, patch: Partial<TierRowState>) =>
    setRows((current) => ({
      ...current,
      [name]: { ...tierRowState(tier), ...current[name], ...patch },
    }))

  const showJudge = mode === 'llm_judge'
  const showPilot = mode === 'pilot-v1'

  const summary = provider ? `${provider} / ${tierLabel(defaultTier)}` : 'Choose a provider first'

  const collectAndSave = () => {
    if (!canSave) return
    const judgeModel = resolveJudgeModelParam(judge, judgeLoaded, judgeIsLocal)
    const params = buildRouterConfigureParams({
      sel: mode,
      defaultTier,
      judgeModel,
      pilotThresholdRaw: pilotThreshold,
      tiers: visibleTiers.map(([name, tier]) => ({ tier: name, ...rowFor(name, tier) })),
    })
    onSave(params)
  }

  return (
    <section className="setup-panel panel">
      <PanelHead title="Router Tiers" subtitle={summary} />
      <div className="setup-router-toolbar">
        <label>
          <span>Mode</span>
          <SetupSelect
            aria-label="Router mode"
            value={mode}
            disabled={!provider}
            onChange={(e) => setMode(e.target.value as RouterMode)}
          >
            <option value="pilot-v1">Local ML - English-optimized (Pilot)</option>
            <option value="llm_judge">Smart routing (LLM-based)</option>
            <option value="disabled">Off</option>
          </SetupSelect>
          {showPilot ? (
            <small className="setup-hint">
              English-optimized local ML router; runs offline with the self-trained AgentOS model.
            </small>
          ) : null}
        </label>
        <label>
          <span>Default text model</span>
          <SetupSelect
            aria-label="Default text model"
            value={defaultTier}
            disabled={!provider}
            onChange={(e) => setDefaultTier(e.target.value)}
          >
            {TEXT_TIERS.map((t) => (
              <option key={t} value={t}>
                {tierLabel(t)}
              </option>
            ))}
          </SetupSelect>
        </label>
        {showJudge ? (
          <label>
            <span>Judge model</span>
            <SetupSelect
              aria-label="Judge model"
              value={judge}
              onChange={(e) => setJudge(e.target.value)}
            >
              <option value="">{judgeAutoLabel}</option>
              {judgeModels.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </SetupSelect>
          </label>
        ) : null}
        {showPilot ? (
          <label>
            <span>Pilot safety net</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              aria-label="Pilot safety-net threshold"
              value={pilotThreshold}
              onChange={(e) => setPilotThreshold(e.target.value)}
            />
            <small className="setup-hint">
              Under-routing floor (default 0.5). The effective cutoff is the max of this and the
              router confidence threshold.
            </small>
          </label>
        ) : null}
      </div>

      {provider ? (
        <div className="setup-tier-table" role="table">
          <div className="setup-tier-table__row is-head" role="row">
            <span role="columnheader">Tier</span>
            <span role="columnheader">Provider</span>
            <span role="columnheader">Model</span>
            <span role="columnheader">Thinking</span>
            <span role="columnheader">Image</span>
          </div>
          {visibleTiers.map(([name, tier]) => {
            // A coherent settings snapshot can add a tier while this mounted
            // editor keeps another tier draft. Seed newly visible rows from
            // their catalog/config spec instead of dereferencing stale state.
            const row = rowFor(name, tier)
            const isImageModel = name === 'image_model'
            const supportsImage = isImageModel || row.supportsImage
            return (
              <div className="setup-tier-table__row" role="row" key={name}>
                <div className="setup-tier-table__cell setup-tier-table__cell--tier" role="cell">
                  <span className="setup-tier-table__mobile-label" aria-hidden="true">
                    Tier
                  </span>
                  <code>{name}</code>
                </div>
                <div className="setup-tier-table__cell" role="cell">
                  <span className="setup-tier-table__mobile-label" aria-hidden="true">
                    Provider
                  </span>
                  <input
                    aria-label={`${name} provider`}
                    value={row.provider}
                    onChange={(e) => setRow(name, tier, { provider: e.target.value })}
                  />
                </div>
                <div className="setup-tier-table__cell setup-tier-table__cell--model" role="cell">
                  <span className="setup-tier-table__mobile-label" aria-hidden="true">
                    Model
                  </span>
                  <input
                    aria-label={`${name} model`}
                    value={row.model}
                    onChange={(e) => setRow(name, tier, { model: e.target.value })}
                  />
                </div>
                <div className="setup-tier-table__cell" role="cell">
                  <span className="setup-tier-table__mobile-label" aria-hidden="true">
                    Thinking
                  </span>
                  <SetupSelect
                    aria-label={`${name} thinking level`}
                    value={row.thinkingLevel}
                    onChange={(e) => setRow(name, tier, { thinkingLevel: e.target.value })}
                  >
                    {THINKING_LEVELS.map((v) => (
                      <option key={v} value={v}>
                        {v || '-'}
                      </option>
                    ))}
                  </SetupSelect>
                </div>
                <div className="setup-tier-table__cell setup-tier-table__cell--image" role="cell">
                  <span className="setup-tier-table__mobile-label" aria-hidden="true">
                    Image
                  </span>
                  <SetupCheckbox
                    ariaLabel={`${name} supports image`}
                    checked={supportsImage}
                    className="setup-check--compact"
                    disabled={isImageModel}
                    onChange={(checked) => setRow(name, tier, { supportsImage: checked })}
                  >
                    {supportsImage ? 'On' : 'Off'}
                  </SetupCheckbox>
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        <div className="setup-warning panel tone-warn tone-rail">
          Choose a provider first to preview and save Pilot Router tiers.
        </div>
      )}

      {provider && !canSave ? (
        <div className="setup-warning panel tone-warn tone-rail">
          Save the provider before saving router tiers.
        </div>
      ) : null}

      <div className="setup-actions">
        <Button type="button" variant="outline" onClick={onBack}>
          Back
        </Button>
        <Button type="button" disabled={!canSave || saving} onClick={collectAndSave}>
          Save Router
        </Button>
        <Button type="button" variant="outline" onClick={onNext}>
          Next
        </Button>
      </div>
    </section>
  )
}

function seedRows(entries: Array<[string, TierSpec]>): Record<string, TierRowState> {
  const rows: Record<string, TierRowState> = {}
  entries.forEach(([name, tier]) => {
    rows[name] = tierRowState(tier)
  })
  return rows
}

function tierRowState(tier: TierSpec | null | undefined): TierRowState {
  return {
    provider: String(tier?.provider || ''),
    model: String(tier?.model || ''),
    thinkingLevel: String(tier?.thinkingLevel || tier?.thinking_level || ''),
    supportsImage: Boolean(tier?.supportsImage || tier?.supports_image),
  }
}
