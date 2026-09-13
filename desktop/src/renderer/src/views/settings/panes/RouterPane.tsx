import { useMutation, useQuery } from '@tanstack/react-query'
import { LoaderCircle } from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import {
  buildRouterConfigureParams,
  classifyRouterModels,
  configuredProvider,
  mergeModelOptions,
  modelOptionLabel,
  offlineTierModels,
  resolveJudgeModelParam,
  TEXT_TIERS,
  type ModelListEntry,
  type ModelOption,
  type RouterMode,
  type SetupConfig,
} from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useGateway } from '~/stores/gateway'
import {
  ROUTER_THINKING,
  routerDirty,
  routerDraft,
  safetyNetValid,
  textTiers,
  type RouterDraft,
  type TierName,
  type TierRow,
} from '../logic'
import { Card, Head, Notice, Pill, Row, Segmented } from '../parts'
import { useConfigSnapshot, withRevision } from '../use-snapshot'

interface ConfigureResult {
  restartRequired?: boolean
  warnings?: string[]
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/**
 * The Pilot Router: routing mode, default tier, the tier ladder. Written
 * through `onboarding.router.configure` with the console's own payload
 * builder, so a router saved here reads identically in the web console.
 */
export function RouterPane() {
  const { connected, query, snapshot, reload } = useConfigSnapshot()
  return (
    <>
      <Head title={t('settings.section.router')} blurb={t('settings.section.router.blurb')} />
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
        <RouterBody snapshot={snapshot} reload={reload} />
      )}
    </>
  )
}

function RouterBody({
  snapshot,
  reload,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
}) {
  const rpc = useRpc()
  const config: SetupConfig = snapshot.config ?? {}
  const catalog = snapshot.catalog ?? {}
  const provider = configuredProvider(snapshot.status ?? {}, config)
  const spec = catalog.providers?.find((p) => p.providerId === provider)
  const profile = catalog.routerProfiles?.profiles?.find((p) => p.providerId === provider)
  const restartGateway = useGateway((s) => s.restart)

  const save = useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      rpc.call<ConfigureResult>('onboarding.router.configure', withRevision(snapshot, params)),
    onSuccess: async (res) => {
      for (const w of res?.warnings ?? []) toast.warning(w)
      toast.success(
        res?.restartRequired ? t('settings.restartRequired') : t('settings.router.saved'),
        { id: 'stg-router' },
      )
      await reload()
    },
    onError: (err) =>
      toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: 'stg-router-err' }),
  })

  if (!provider) return <Notice tone="info">{t('settings.router.noProvider')}</Notice>
  if (!profile && (!spec || spec.routerSupported !== true)) {
    return <Notice tone="info">{t('settings.router.directOnly')}</Notice>
  }

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
      <RouterForm
        key={`${provider}:${snapshot.revision ?? ''}`}
        snapshot={snapshot}
        provider={provider}
        saving={save.isPending}
        disabled={Boolean(snapshot.writeBlocked)}
        onSave={(params) => save.mutate(params)}
      />
    </>
  )
}

/** The id the config file holds, plus the context window when known. */
function tierOptionLabel(o: ModelOption): string {
  const ctx = Number(o.contextWindow || 0)
  return ctx > 0 ? `${o.id}  ·  ${Math.round(ctx / 1000)}k` : o.id
}

const MODE_KEY: Record<RouterMode, 'pilot' | 'judge' | 'off'> = {
  'pilot-v1': 'pilot',
  llm_judge: 'judge',
  disabled: 'off',
}

function RouterForm({
  snapshot,
  provider,
  saving,
  disabled,
  onSave,
}: {
  snapshot: SettingsSnapshot
  provider: string
  saving: boolean
  disabled: boolean
  onSave: (params: Record<string, unknown>) => void
}) {
  const rpc = useRpc()
  const config: SetupConfig = snapshot.config ?? {}
  const catalog = snapshot.catalog ?? {}
  const router = config.agentos_router || {}
  const profile = catalog.routerProfiles?.profiles?.find((p) => p.providerId === provider)
  const saved = routerDraft(config, catalog, provider)
  const [draft, setDraft] = useState<RouterDraft>(saved)
  const dirty = routerDirty(saved, draft)
  const ids = { safety: useId(), judge: useId(), translate: useId() }

  const judgeProfile = catalog.routerProfiles?.judge?.profiles?.[provider]
  const judgeModels = judgeProfile?.models ?? []
  const judgeAuto = judgeProfile?.autoModel ?? null
  const judgeLoaded = router.judge_model || ''
  const judgeIsLocal = Boolean(router.judge_base_url)

  const hasImage = draft.tiers.some((row) => row.tier === 'image_model')
  const textQuery = useQuery({
    queryKey: ['settings', 'models', provider],
    staleTime: 60_000,
    retry: false,
    queryFn: () => rpc.call<ModelListEntry[]>('models.list', { provider }),
  })
  const visionQuery = useQuery({
    queryKey: ['settings', 'models', provider, 'vision'],
    enabled: hasImage,
    staleTime: 60_000,
    retry: false,
    queryFn: () =>
      rpc.call<ModelListEntry[]>('models.list', { provider, capabilities: ['vision'] }),
  })
  const textOptions = mergeModelOptions(textQuery.data, offlineTierModels(profile?.tiers))
  const visionOptions = mergeModelOptions(
    visionQuery.data,
    offlineTierModels(profile?.tiers, { visionOnly: true }),
  )

  const setTier = (tier: TierName, patch: Partial<TierRow>) =>
    setDraft((d) => ({
      ...d,
      tiers: d.tiers.map((row) => (row.tier === tier ? { ...row, ...patch } : row)),
    }))

  const safetyOk = draft.mode !== 'pilot-v1' || safetyNetValid(draft.safetyNet)
  const canSave = dirty && safetyOk && !saving && !disabled

  function submit() {
    const warnings = classifyRouterModels(
      draft.tiers.map((row) => ({ tier: row.tier, model: row.model })),
      textOptions,
      visionOptions,
    )
    if (warnings.unknown.length > 0) {
      toast.warning(`${t('settings.router.unknownModels')} ${warnings.unknown.join(', ')}`, {
        id: 'stg-router-unknown',
      })
    }
    if (warnings.nonVision.length > 0) {
      toast.warning(`${t('settings.router.nonVision')} ${warnings.nonVision.join(', ')}`, {
        id: 'stg-router-vision',
      })
    }
    const params = buildRouterConfigureParams({
      sel: draft.mode,
      defaultTier: draft.defaultTier,
      judgeModel: resolveJudgeModelParam(draft.judgeModel, judgeLoaded, judgeIsLocal),
      pilotThresholdRaw: draft.safetyNet,
      translateCeilingEnabled: draft.translateCeiling !== 'off',
      translateCeilingTier: draft.translateCeiling === 'off' ? 'c0' : draft.translateCeiling,
      tiers: draft.tiers.map((row) => ({
        tier: row.tier,
        provider,
        model: row.model,
        thinkingLevel: row.thinkingLevel,
        supportsImage: row.supportsImage,
      })),
    })
    onSave(params as unknown as Record<string, unknown>)
  }

  const modeHelp = t(`settings.router.mode.${MODE_KEY[draft.mode]}.help`)
  const routingOn = draft.mode !== 'disabled'

  return (
    <>
      <Card
        title={t('settings.router.mode')}
        blurb={t('settings.router.mode.blurb')}
        action={<Pill tone={routingOn ? 'ok' : undefined}>{provider}</Pill>}
      >
        <Row label={t('settings.router.modeRow')} help={modeHelp}>
          <Segmented
            label={t('settings.router.mode')}
            value={draft.mode}
            disabled={disabled}
            options={[
              { value: 'pilot-v1', label: t('settings.router.mode.pilot') },
              { value: 'llm_judge', label: t('settings.router.mode.judge') },
              { value: 'disabled', label: t('settings.router.mode.off') },
            ]}
            onChange={(mode) => setDraft((d) => ({ ...d, mode }))}
          />
        </Row>
        <Row label={t('settings.router.defaultTier')} help={t('settings.router.defaultTier.help')}>
          <select
            className="mac-select"
            data-compact="true"
            aria-label={t('settings.router.defaultTier')}
            value={draft.defaultTier}
            disabled={disabled}
            onChange={(e) => setDraft((d) => ({ ...d, defaultTier: e.target.value }))}
          >
            {textTiers(draft).map((row) => (
              <option key={row.tier} value={row.tier}>
                {row.tier} · {t(`settings.router.tier.${row.tier}`)}
              </option>
            ))}
          </select>
        </Row>
        {draft.mode === 'pilot-v1' ? (
          <Row
            label={t('settings.router.safetyNet')}
            help={t('settings.router.safetyNet.help')}
            htmlFor={ids.safety}
          >
            <span className="stg-slider">
              <input
                id={ids.safety}
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={safetyNetValid(draft.safetyNet) ? Number(draft.safetyNet) : 0.5}
                disabled={disabled}
                onChange={(e) => setDraft((d) => ({ ...d, safetyNet: e.target.value }))}
              />
              <output htmlFor={ids.safety}>{Number(draft.safetyNet).toFixed(2)}</output>
            </span>
          </Row>
        ) : null}
        {draft.mode === 'llm_judge' ? (
          <Row
            label={t('settings.router.judge')}
            help={t('settings.router.judge.help')}
            htmlFor={ids.judge}
          >
            <select
              id={ids.judge}
              className="mac-select"
              value={draft.judgeModel}
              disabled={disabled}
              onChange={(e) => setDraft((d) => ({ ...d, judgeModel: e.target.value }))}
            >
              <option value="">
                {t('settings.router.judge.auto')}
                {judgeAuto ? ` · ${judgeAuto}` : ''}
              </option>
              {judgeModels.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </Row>
        ) : null}
        <Row
          label={t('settings.router.translate')}
          help={t('settings.router.translate.help')}
          htmlFor={ids.translate}
        >
          <select
            id={ids.translate}
            className="mac-select"
            data-compact="true"
            value={draft.translateCeiling}
            disabled={disabled}
            onChange={(e) => setDraft((d) => ({ ...d, translateCeiling: e.target.value }))}
          >
            <option value="off">{t('settings.router.translate.off')}</option>
            {TEXT_TIERS.map((tier) => (
              <option key={tier} value={tier}>
                {tier} · {t(`settings.router.tier.${tier}`)}
              </option>
            ))}
          </select>
        </Row>
      </Card>

      <Card
        title={t('settings.router.tiers')}
        blurb={t('settings.router.tiers.blurb')}
        foot={
          <>
            <Button disabled={!dirty || saving} onClick={() => setDraft(saved)}>
              {t('settings.revert')}
            </Button>
            <Button variant="primary" disabled={!canSave} onClick={submit}>
              {t('settings.save')}
            </Button>
          </>
        }
      >
        <div className="stg-tiers" role="list">
          {draft.tiers.map((row) => {
            const isImage = row.tier === 'image_model'
            const options = isImage ? visionOptions : textOptions
            const known = options.some((o) => o.id === row.model)
            const isDefault = !isImage && row.tier === draft.defaultTier
            return (
              <div key={row.tier} className="stg-tier" role="listitem" data-default={isDefault}>
                <div className="stg-tier__rung">
                  <span className="stg-tier__code">{row.tier}</span>
                  <span className="stg-tier__name">{t(`settings.router.tier.${row.tier}`)}</span>
                  {!isImage ? (
                    <button
                      type="button"
                      role="radio"
                      aria-checked={isDefault}
                      className="stg-tier__default app-no-drag"
                      disabled={disabled}
                      onClick={() => setDraft((d) => ({ ...d, defaultTier: row.tier }))}
                    >
                      {isDefault
                        ? t('settings.router.tier.default')
                        : t('settings.router.tier.makeDefault')}
                    </button>
                  ) : null}
                </div>
                <div className="stg-tier__model">
                  <span className="stg-tier__caption">{t('settings.router.tier.model')}</span>
                  <select
                    className="mac-select"
                    aria-label={`${row.tier} ${t('settings.router.tier.model')}`}
                    value={row.model}
                    disabled={disabled}
                    onChange={(e) => setTier(row.tier, { model: e.target.value })}
                  >
                    {!known && row.model ? <option value={row.model}>{row.model}</option> : null}
                    {options.map((o) => (
                      <option key={o.id} value={o.id} title={modelOptionLabel(o)}>
                        {tierOptionLabel(o)}
                      </option>
                    ))}
                  </select>
                  {row.description ? (
                    <span className="stg-tier__desc">{row.description}</span>
                  ) : null}
                </div>
                <div className="stg-tier__think">
                  <span className="stg-tier__caption">{t('settings.router.tier.thinking')}</span>
                  <select
                    className="mac-select"
                    aria-label={`${row.tier} ${t('settings.router.tier.thinking')}`}
                    value={row.thinkingLevel}
                    disabled={disabled}
                    onChange={(e) => setTier(row.tier, { thinkingLevel: e.target.value })}
                  >
                    {ROUTER_THINKING.map((level) => (
                      <option key={level} value={level}>
                        {level === ''
                          ? t('settings.router.tier.thinking.inherit')
                          : t(`settings.models.thinking.${level}`)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            )
          })}
        </div>
      </Card>
    </>
  )
}
