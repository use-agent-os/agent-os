import { useMutation, useQuery } from '@tanstack/react-query'
import { CircleOff, Gauge, LoaderCircle, Scale, ShieldAlert } from 'lucide-react'
import { useId, useRef } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import {
  classifyRouterModels,
  configuredProvider,
  mergeModelOptions,
  modelOptionLabel,
  offlineTierModels,
  TEXT_TIERS,
  type ModelListEntry,
  type ModelOption,
  type RouterMode,
  type SetupConfig,
} from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { t, type MessageKey } from '~/i18n'
import {
  ROUTER_THINKING,
  routerChanges,
  routerDirty,
  routerDraft,
  routerDraftId,
  routerPatch,
  safetyNetValid,
  type TierName,
  type TierRow,
} from '../logic'
import { useDraft, useDraftStore } from '../memory/drafts'
import { Card, Choices, Disclosure, Head, Notice, Pill, Row } from '../parts'
import { PendingRestartNotice } from '../PendingRestartNotice'
import { useConfigSnapshot, withRevision } from '../use-snapshot'
import { keepFocus, useArrivalFocus } from './focus'

interface PatchResult {
  restartRequired?: boolean
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/**
 * The Pilot Router: routing mode, default tier, the tier ladder. Written
 * through `config.patch` with the snapshot revision, carrying only the keys
 * the edit changed (`routerPatch` says why not the guided write).
 */
export function RouterPane() {
  useArrivalFocus('router')
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
  const formRef = useRef<HTMLDivElement>(null)

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) =>
      rpc.call<PatchResult>('config.patch', withRevision(snapshot, { patch })),
    onSuccess: async (res) => {
      // Saved: the next form is the file's, not the parked edit.
      useDraftStore.getState().drop(routerDraftId(provider))
      toast.success(
        res?.restartRequired ? t('settings.restartRequired') : t('settings.router.saved'),
        { id: 'stg-router' },
      )
      await reload()
      // The save bar goes with the change, and the form re-mounts on the new
      // revision: keep keyboard focus in the sheet.
      keepFocus(formRef.current)
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
        <PendingRestartNotice />
      ) : null}
      <span ref={formRef} hidden />
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
  // Parked outside the form, so leaving the section or closing the sheet
  // keeps the edit and its save bar for when the page is opened again.
  const { draft, setDraft, revert } = useDraft(routerDraftId(provider), saved)
  const dirty = routerDirty(saved, draft)
  const ids = {
    safety: useId(),
    judge: useId(),
    translate: useId(),
    jevKey: useId(),
    jevRisk: useId(),
  }

  const judgeProfile = catalog.routerProfiles?.judge?.profiles?.[provider]
  const judgeModels = judgeProfile?.models ?? []
  const judgeAuto = judgeProfile?.autoModel ?? null
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
  // Until the provider's own list arrives the menu holds only the offline
  // profile models; say so instead of passing them off as the whole catalog.
  const listStatus = (query: { isLoading: boolean; isError: boolean }) =>
    query.isLoading
      ? t('settings.router.tier.modelsLoading')
      : query.isError
        ? t('settings.router.tier.modelsFailed')
        : ''
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
  const jevOk = draft.mode !== 'jev' || safetyNetValid(draft.jevHighRisk)
  const canSave = dirty && safetyOk && jevOk && !saving && !disabled
  const changes = routerChanges(saved, draft)

  function submit() {
    const patch = routerPatch(saved, draft, { judgeIsLocal })
    if (!patch) return
    const warnings = changes.includes('tiers')
      ? classifyRouterModels(
          draft.tiers.map((row) => ({ tier: row.tier, model: row.model })),
          textOptions,
          visionOptions,
        )
      : { unknown: [], nonVision: [] }
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
    onSave(patch)
  }

  const routingOn = draft.mode !== 'disabled'
  const providerLabel = catalog.providers?.find((p) => p.providerId === provider)?.label ?? provider
  const tuning = [
    draft.mode === 'pilot-v1'
      ? `${t('settings.router.safetyNet')} ${Number(draft.safetyNet).toFixed(2)}`
      : null,
    draft.mode === 'jev'
      ? `${t('settings.router.jevRisk')} ${Number(draft.jevHighRisk).toFixed(2)}`
      : null,
    `${t('settings.router.translate')} ${
      draft.translateCeiling === 'off' ? t('settings.router.translate.off') : draft.translateCeiling
    }`,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <>
      <Card
        title={t('settings.router.mode')}
        blurb={t('settings.router.mode.blurb')}
        action={<Pill tone={routingOn ? 'ok' : undefined}>{providerLabel}</Pill>}
      >
        <Choices<RouterMode>
          label={t('settings.router.modeRow')}
          value={draft.mode}
          disabled={disabled}
          options={[
            {
              value: 'pilot-v1',
              title: t('settings.router.mode.pilot'),
              blurb: t('settings.router.mode.pilot.help'),
              icon: <Gauge strokeWidth={1.75} aria-hidden />,
              tag: t('settings.router.mode.recommended'),
              tagTone: 'ok',
            },
            {
              value: 'llm_judge',
              title: t('settings.router.mode.judge'),
              blurb: t('settings.router.mode.judge.help'),
              icon: <Scale strokeWidth={1.75} aria-hidden />,
            },
            {
              value: 'jev',
              title: t('settings.router.mode.jev'),
              blurb: t('settings.router.mode.jev.help'),
              icon: <ShieldAlert strokeWidth={1.75} aria-hidden />,
              tag: t('settings.router.mode.experimental'),
              tagTone: 'warn',
            },
            {
              value: 'disabled',
              title: t('settings.router.mode.off'),
              blurb: t('settings.router.mode.off.help'),
              icon: <CircleOff strokeWidth={1.75} aria-hidden />,
            },
          ]}
          onChange={(mode) => setDraft((d) => ({ ...d, mode }))}
        />
        {draft.mode === 'jev' ? (
          <Row
            label={t('settings.router.jevKey')}
            help={t('settings.router.jevKey.help')}
            htmlFor={ids.jevKey}
          >
            <input
              id={ids.jevKey}
              className="mac-input"
              data-mono="true"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={t('settings.router.jevKey.placeholder')}
              value={draft.jevApiKey}
              disabled={disabled}
              onChange={(e) => setDraft((d) => ({ ...d, jevApiKey: e.target.value }))}
            />
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
        <Disclosure title={t('settings.router.tuning')} hint={tuning}>
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
          {draft.mode === 'jev' ? (
            <Row
              label={t('settings.router.jevRisk')}
              help={t('settings.router.jevRisk.help')}
              htmlFor={ids.jevRisk}
            >
              <span className="stg-slider">
                <input
                  id={ids.jevRisk}
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={safetyNetValid(draft.jevHighRisk) ? Number(draft.jevHighRisk) : 0.7}
                  disabled={disabled}
                  onChange={(e) => setDraft((d) => ({ ...d, jevHighRisk: e.target.value }))}
                />
                <output htmlFor={ids.jevRisk}>{Number(draft.jevHighRisk).toFixed(2)}</output>
              </span>
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
        </Disclosure>
      </Card>

      <Card title={t('settings.router.tiers')} blurb={t('settings.router.tiers.blurb')}>
        <div className="stg-tiers" role="list">
          <div className="stg-tier stg-tier--head" aria-hidden>
            <span className="stg-tier__caption">{t('settings.router.tier.rung')}</span>
            <span className="stg-tier__caption">{t('settings.router.tier.model')}</span>
            <span className="stg-tier__caption">{t('settings.router.tier.thinking')}</span>
          </div>
          {draft.tiers.map((row) => {
            const isImage = row.tier === 'image_model'
            const options = isImage ? visionOptions : textOptions
            const status = listStatus(isImage ? visionQuery : textQuery)
            const known = options.some((o) => o.id === row.model)
            const isDefault = !isImage && row.tier === draft.defaultTier
            return (
              <div key={row.tier} className="stg-tier" role="listitem" data-default={isDefault}>
                <div className="stg-tier__rung">
                  <span className="stg-tier__label">
                    <span className="stg-tier__code">{isImage ? 'img' : row.tier}</span>
                    <span className="stg-tier__name">{t(`settings.router.tier.${row.tier}`)}</span>
                  </span>
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
                    {status ? (
                      <option value="" disabled>
                        {status}
                      </option>
                    ) : null}
                  </select>
                  {row.description ? (
                    <span className="stg-tier__desc">{row.description}</span>
                  ) : null}
                </div>
                <div className="stg-tier__think">
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

      {dirty ? (
        <SaveBar
          changes={changes.map((c) => t(CHANGE_LABEL[c]))}
          saving={saving}
          canSave={canSave}
          blockedReason={disabled ? t('settings.writeBlocked') : null}
          onRevert={revert}
          onSave={submit}
        />
      ) : null}
    </>
  )
}

const CHANGE_LABEL: Record<ReturnType<typeof routerChanges>[number], MessageKey> = {
  mode: 'settings.router.mode',
  tuning: 'settings.router.tuning',
  tiers: 'settings.router.tiers',
}

/**
 * The page's one Save: it rides the bottom of the sheet while anything on
 * the page is unsaved, so the change and its Save are on screen together
 * wherever the change was made (the routing mode at the top, a tier at the
 * bottom). Nothing is shown while the page matches what is saved.
 */
function SaveBar({
  changes,
  saving,
  canSave,
  blockedReason,
  onRevert,
  onSave,
}: {
  /** What is unsaved, by card title. */
  changes: string[]
  saving: boolean
  canSave: boolean
  blockedReason: string | null
  onRevert: () => void
  onSave: () => void
}) {
  return (
    <div className="stg-savebar" role="region" aria-label={t('settings.router.unsaved')}>
      <span className="stg-savebar__dot" aria-hidden />
      <span className="stg-savebar__text">
        <span className="stg-savebar__title">{t('settings.router.unsaved')}</span>
        {changes.length > 0 ? (
          <span className="stg-savebar__what" data-testid="router-unsaved">
            {changes.join(' · ')}
          </span>
        ) : null}
      </span>
      <Button
        disabled={saving}
        onClick={(e) => {
          // The bar goes with the change it reverted: keep focus in the sheet.
          const sheet = e.currentTarget.closest('[role="dialog"]')
          onRevert()
          keepFocus(sheet)
        }}
      >
        {t('settings.revert')}
      </Button>
      <Button
        variant="primary"
        disabled={!canSave}
        title={blockedReason ?? undefined}
        onClick={onSave}
      >
        {saving ? <LoaderCircle className="stg-spin size-3.5" strokeWidth={2} aria-hidden /> : null}
        {t('settings.save')}
      </Button>
    </div>
  )
}
