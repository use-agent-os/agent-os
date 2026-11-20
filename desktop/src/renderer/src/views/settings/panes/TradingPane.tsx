import { useMutation } from '@tanstack/react-query'
import { AlertTriangle, Check, ExternalLink, Eye, EyeOff, LoaderCircle } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { useSettings } from '~/stores/settings'
import { useProbe, useTradingStatus, useWalletMutation, useWalletStatus } from '~/stores/trading'
import { errorText } from '~/views/trading/logic'
import { ProviderMark } from '~/views/trading/ProviderMark'
import {
  DEFAULT_PROVIDER,
  PROVIDERS,
  type ProviderId,
  type ProviderStatus,
  type UnlockMode,
} from '~/views/trading/types'
import { jumpTo } from '../overview/jumps'
import { Card, Choices, Head, Notice, Pill, Row, Segmented, Stat, Stats, Value } from '../parts'
import { useConfigSnapshot, withRevision } from '../use-snapshot'
import { clearOnEscape, keepFocus, sheetOf, useArrivalFocus } from './focus'
import { isOperatorRefusal, readOnlyAdvice, useTradingAccess } from './trading-access'

export const UNISWAP_DASHBOARD_URL = 'https://developers.uniswap.org/dashboard'

interface TradingConfig {
  enabled?: boolean
  uniswap_api_key?: string
  uniswap_api_key_env?: string
  rpc_urls?: Record<string, string>
  approval_threshold_usd?: number
  daily_cap_usd?: number
  approval_ttl_seconds?: number
  default_slippage_pct?: number | null
  unlock_mode?: UnlockMode
  provider?: ProviderId
}

function isProviderId(value: unknown): value is ProviderId {
  return PROVIDERS.some((p) => p.id === value)
}

/** The keyless default: one button says whether it answers from here. */
function AggregatorRow({ status }: { status: ProviderStatus | undefined }) {
  const probe = useProbe()
  const result = probe.data
  const verdict: 'idle' | 'checking' | 'ok' | 'bad' = probe.isPending
    ? 'checking'
    : probe.isError
      ? 'bad'
      : result
        ? result.ok
          ? 'ok'
          : 'bad'
        : status?.healthy === false
          ? 'bad'
          : 'idle'
  const detail = probe.error ? errorText(probe.error) : (result?.error ?? '')
  return (
    <Row
      label={t('settings.trading.aggregator.check')}
      help={
        <>
          {verdict === 'idle' ? <span>{t('settings.trading.aggregator.check.help')}</span> : null}
          {verdict !== 'idle' ? (
            <span className="trd-stg__probe" data-verdict={verdict} data-testid="aggregator-probe">
              {verdict === 'checking' ? (
                <>
                  <LoaderCircle className="stg-spin size-3" strokeWidth={2} aria-hidden />
                  {t('trading.settings.aggregator.testing')}
                </>
              ) : verdict === 'ok' ? (
                <>
                  <Check className="size-3" strokeWidth={2.5} aria-hidden />
                  {t('trading.settings.aggregator.works')}
                  {result?.latencyMs ? ` ${result.latencyMs} ms` : ''}
                </>
              ) : (
                <>
                  <AlertTriangle className="size-3" strokeWidth={2} aria-hidden />
                  {t('trading.settings.aggregator.failed')} <code>{detail}</code>
                </>
              )}
            </span>
          ) : null}
        </>
      }
      align="start"
    >
      <Button
        disabled={probe.isPending}
        onClick={() => probe.mutate({ provider: 'aggregator' })}
        data-testid="aggregator-test"
      >
        {probe.isPending
          ? t('trading.settings.aggregator.testing')
          : t('trading.settings.aggregator.test')}
      </Button>
    </Row>
  )
}

interface PatchResult {
  restartRequired?: boolean
}

/**
 * Settings › Trading: the Uniswap key with a Test button, the vault's
 * state and unlock mode, the agent's limits, and each network's RPC.
 * Written through `config.patch` with the snapshot revision like every
 * other guided write; the vault itself is written through wallet.*.
 */
export function TradingPane() {
  useArrivalFocus('trading')
  const { connected, query, snapshot, reload } = useConfigSnapshot()
  const head = (action?: React.ReactNode) => (
    <Head
      title={t('settings.section.trading')}
      blurb={t('settings.section.trading.blurb')}
      action={action}
    />
  )
  if (connected && snapshot) return <TradingBody snapshot={snapshot} reload={reload} head={head} />
  return (
    <>
      {head()}
      {!connected ? (
        <Notice tone="info">{t('settings.offline')}</Notice>
      ) : query.isError ? (
        <Notice tone="danger">{t('settings.loadFailed')}</Notice>
      ) : (
        <div className="flex items-center gap-2 text-muted-foreground">
          <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
          {t('settings.loading')}
        </div>
      )}
    </>
  )
}

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
})

function TradingBody({
  snapshot,
  reload,
  head,
}: {
  snapshot: SettingsSnapshot
  reload: () => Promise<void>
  head: (action?: React.ReactNode) => React.ReactNode
}) {
  const rpc = useRpc()
  const cfg = ((snapshot.config as Record<string, unknown> | undefined)?.trading ??
    {}) as TradingConfig
  const status = useTradingStatus()
  const vault = useWalletStatus()
  const provider: ProviderId = isProviderId(cfg.provider)
    ? cfg.provider
    : (status.data?.provider ?? DEFAULT_PROVIDER)

  // Known up front when the gateway says so; otherwise learnt from the first
  // write it refuses for that reason. Either way the page says it once, at
  // the top, and every control is read-only with that reason on it.
  const access = useTradingAccess()
  const gateway = useSettings((s) => s.settings.gateway)
  const [refused, setRefused] = useState(false)
  const readOnly = access === 'agent' || refused
  const lockReason = readOnly
    ? t('settings.trading.readOnly.reason')
    : snapshot.writeBlocked
      ? t('settings.writeBlocked')
      : null
  const locked = lockReason !== null

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) =>
      rpc.call<PatchResult>('config.patch', withRevision(snapshot, { patch: { trading: patch } })),
    onSuccess: async (res, patch) => {
      toast.success(
        res?.restartRequired
          ? t('settings.restartRequired')
          : 'provider' in patch
            ? t('trading.settings.provider.saved')
            : t('trading.settings.saved'),
        {
          id: 'stg-trading',
        },
      )
      await reload()
      await status.refetch()
    },
    onError: (err) => {
      if (isOperatorRefusal(err)) {
        setRefused(true)
        toast.error(t('settings.trading.readOnly.refused'), { id: 'stg-trading-err' })
      } else {
        toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: 'stg-trading-err' })
      }
    },
  })
  /** True once the write landed; a field that sent it puts the saved value back on false. */
  const write = (patch: Record<string, unknown>) =>
    save.mutateAsync(patch).then(
      () => true,
      () => false,
    )

  const enabled = cfg.enabled !== false
  const vaultState = !vault.data?.initialized ? 'none' : vault.data.unlocked ? 'unlocked' : 'locked'
  const providerName = PROVIDERS.find((p) => p.id === provider)?.label ?? provider

  return (
    <>
      {head(
        <label className="trd-switch" title={lockReason ?? t('trading.settings.enabled.help')}>
          <span>{enabled ? t('settings.trading.on') : t('settings.trading.off')}</span>
          {/* Not disabled while saving: greying out the focused switch would drop
              focus out of the sheet. A second flip waits for the first instead. */}
          <Switch
            checked={enabled}
            disabled={locked}
            aria-busy={save.isPending || undefined}
            aria-label={t('trading.settings.enabled')}
            onCheckedChange={(next) => {
              if (!save.isPending) save.mutate({ enabled: next })
            }}
          />
        </label>,
      )}

      {readOnly ? (
        <Notice
          action={
            <Button
              onClick={() => {
                jumpTo('gateway')
                // This page (and the button) goes; the rail's Gateway item takes focus.
                keepFocus(null, () => document.getElementById('stg-rail-gateway'))
              }}
              data-testid="trading-readonly-open"
            >
              {t('settings.trading.readOnly.open')}
            </Button>
          }
        >
          <span className="stg-notice__title" data-testid="trading-readonly">
            {t('settings.trading.readOnly.title')}
          </span>
          {t(`settings.trading.readOnly.${readOnlyAdvice(gateway)}`)}
        </Notice>
      ) : snapshot.writeBlocked ? (
        <Notice tone="danger">{t('settings.writeBlocked')}</Notice>
      ) : null}
      {!enabled ? <Notice tone="info">{t('settings.trading.offNotice')}</Notice> : null}

      <Stats label={t('settings.trading.stats')}>
        <Stat
          label={t('trading.settings.provider')}
          value={providerName}
          icon={<ProviderMark id={provider} size={15} />}
        />
        <Stat
          label={t('trading.settings.vault.wallets')}
          value={vault.data ? vault.data.walletCount : '—'}
          hint={vault.data ? t(`trading.settings.vault.state.${vaultState}`) : undefined}
        />
        <Stat
          label={t('trading.settings.threshold')}
          value={USD.format(cfg.approval_threshold_usd ?? 100)}
          hint={t('settings.trading.perSwap')}
        />
        <Stat
          label={t('trading.settings.dailyCap')}
          value={USD.format(cfg.daily_cap_usd ?? 1000)}
          hint={t('settings.trading.perWallet')}
        />
      </Stats>

      <Card title={t('trading.settings.provider')} blurb={t('settings.trading.provider.blurb')}>
        <div title={lockReason ?? undefined}>
          <Choices<ProviderId>
            label={t('trading.settings.provider')}
            value={provider}
            disabled={locked}
            options={PROVIDERS.map((p) => ({
              value: p.id,
              title: p.label,
              blurb:
                p.id === 'aggregator'
                  ? t('trading.settings.aggregator.blurb')
                  : t('settings.trading.uniswap.blurb'),
              icon: <ProviderMark id={p.id} size={16} />,
              tag: p.id === DEFAULT_PROVIDER ? t('settings.trading.default') : undefined,
              tagTone: 'ok' as const,
            }))}
            onChange={(next) => {
              if (next !== provider && !save.isPending) save.mutate({ provider: next })
            }}
          />
        </div>
        <AggregatorRow status={status.data?.providers?.find((p) => p.id === 'aggregator')} />
      </Card>

      <KeyCard
        cfg={cfg}
        configured={Boolean(status.data?.apiKeyConfigured)}
        lockReason={lockReason}
        saving={save.isPending}
        idle={provider !== 'uniswap'}
        onSave={(k) => save.mutate({ uniswap_api_key: k })}
      />

      <VaultCard
        unlockMode={vault.data?.unlockMode ?? cfg.unlock_mode ?? 'auto'}
        initialized={Boolean(vault.data?.initialized)}
        vaultPath={vault.data?.vaultPath ?? ''}
        lockReason={lockReason}
        onRefused={() => setRefused(true)}
      />

      <LimitsCard cfg={cfg} lockReason={lockReason} saving={save.isPending} onSave={write} />

      <Card title={t('trading.settings.chains')} blurb={t('trading.settings.chains.blurb')}>
        {(status.data?.chains ?? []).map((c) => (
          <RpcRow
            key={c.chainId}
            name={c.name}
            chainId={c.chainId}
            healthy={c.healthy}
            value={cfg.rpc_urls?.[String(c.chainId)] ?? ''}
            placeholder={c.rpcUrl}
            lockReason={lockReason}
            saving={save.isPending}
            onSave={(url) =>
              write({ rpc_urls: { ...(cfg.rpc_urls ?? {}), [String(c.chainId)]: url } })
            }
          />
        ))}
      </Card>
    </>
  )
}

function KeyCard({
  cfg,
  configured,
  lockReason,
  saving,
  idle,
  onSave,
}: {
  cfg: TradingConfig
  configured: boolean
  /** Why the key cannot be changed here; null when it can. */
  lockReason: string | null
  saving: boolean
  /** Another provider is selected: the key is kept but not in use. */
  idle: boolean
  onSave: (key: string) => void
}) {
  const id = useId()
  const [key, setKey] = useState('')
  const [show, setShow] = useState(false)
  const probe = useProbe()
  const verdict: 'idle' | 'checking' | 'ok' | 'bad' = probe.isPending
    ? 'checking'
    : probe.isError
      ? 'bad'
      : probe.data
        ? probe.data.ok
          ? 'ok'
          : 'bad'
        : 'idle'
  const detail = probe.error ? errorText(probe.error) : (probe.data?.error ?? '')
  const hasStored = configured || Boolean(cfg.uniswap_api_key)
  const locked = lockReason !== null
  const nothingToTest = !key.trim() && !hasStored

  return (
    <Card
      title={t('trading.settings.uniswap')}
      blurb={
        <>
          {t('trading.settings.uniswap.blurb')}{' '}
          <button
            type="button"
            className="trd-stg__keylink"
            onClick={() => void desktopApi().app.openExternal(UNISWAP_DASHBOARD_URL)}
          >
            {t('trading.settings.key.get')}
            <ExternalLink className="size-3" strokeWidth={2} aria-hidden />
          </button>
        </>
      }
      action={
        idle ? (
          <Pill>{t('settings.trading.key.notInUse')}</Pill>
        ) : hasStored ? (
          <Pill tone="ok">{t('settings.providers.state.active')}</Pill>
        ) : (
          <Pill>{t('settings.providers.state.needsKey')}</Pill>
        )
      }
      foot={
        <>
          <Button disabled={!key.trim() || saving} onClick={() => setKey('')}>
            {t('settings.revert')}
          </Button>
          <Button
            variant="primary"
            disabled={!key.trim() || saving || locked}
            title={lockReason ?? undefined}
            onClick={(e) => {
              onSave(key.trim())
              setKey('')
              // Saved, this button greys out: focus goes back to the key field.
              keepFocus(sheetOf(e), () => document.getElementById(id))
            }}
            data-testid="trading-key-save"
          >
            {t('settings.save')}
          </Button>
        </>
      }
    >
      <Row
        label={t('trading.settings.key')}
        htmlFor={id}
        align="start"
        help={
          <>
            <span className={idle ? 'text-dim' : undefined} data-testid="uniswap-key-hint">
              {idle
                ? t('trading.settings.key.idle')
                : hasStored
                  ? t('trading.settings.key.saved')
                  : t('trading.settings.key.help')}
            </span>
            {verdict !== 'idle' ? (
              <span
                className="trd-stg__probe"
                data-verdict={verdict}
                data-testid="trading-key-probe"
              >
                {verdict === 'checking' ? (
                  <>
                    <LoaderCircle className="stg-spin size-3" strokeWidth={2} aria-hidden />
                    {t('trading.settings.key.testing')}
                  </>
                ) : verdict === 'ok' ? (
                  <>
                    <Check className="size-3" strokeWidth={2.5} aria-hidden />
                    {t('trading.settings.key.works')}
                    {probe.data?.latencyMs ? ` ${probe.data.latencyMs} ms` : ''}
                  </>
                ) : (
                  <>
                    <AlertTriangle className="size-3" strokeWidth={2} aria-hidden />
                    {t('trading.settings.key.failed')} <code>{detail}</code>
                  </>
                )}
              </span>
            ) : null}
          </>
        }
      >
        <span className="stg-input-wrap">
          <input
            id={id}
            className="mac-input"
            data-mono="true"
            type={show ? 'text' : 'password'}
            autoComplete="off"
            spellCheck={false}
            placeholder={hasStored ? '••••••••••••' : t('trading.settings.key.placeholder')}
            value={key}
            disabled={locked}
            title={lockReason ?? undefined}
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => clearOnEscape(e, key, () => setKey(''))}
          />
          <Button
            variant="ghost"
            size="icon"
            disabled={locked}
            title={lockReason ?? undefined}
            aria-label={show ? t('trading.settings.key.hide') : t('trading.settings.key.show')}
            aria-pressed={show}
            onClick={() => setShow((v) => !v)}
          >
            {show ? (
              <EyeOff className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            ) : (
              <Eye className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            )}
          </Button>
        </span>
        <Button
          disabled={probe.isPending || nothingToTest}
          title={nothingToTest ? t('settings.trading.key.testNeedsKey') : undefined}
          onClick={() => probe.mutate({ provider: 'uniswap', apiKey: key.trim() || undefined })}
          data-testid="trading-key-test"
        >
          {probe.isPending ? t('trading.settings.key.testing') : t('trading.settings.key.test')}
        </Button>
      </Row>
    </Card>
  )
}

function VaultCard({
  unlockMode,
  initialized,
  vaultPath,
  lockReason,
  onRefused,
}: {
  unlockMode: UnlockMode
  initialized: boolean
  vaultPath: string
  lockReason: string | null
  /** The gateway refused the write because of who is asking. */
  onRefused: () => void
}) {
  const id = useId()
  const [pendingMode, setPendingMode] = useState<UnlockMode | null>(null)
  const [password, setPassword] = useState('')
  const write = useWalletMutation()
  const cardRef = useRef<HTMLSpanElement>(null)
  // The vault is made with the first wallet; until then there is nothing to unlock.
  const reason = lockReason ?? (initialized ? null : t('settings.trading.vault.needsVault'))
  return (
    <Card title={t('trading.settings.vault')} blurb={t('trading.settings.vault.blurb')}>
      {vaultPath ? (
        <Row label={t('trading.settings.vault.path')}>
          <Value title={vaultPath}>{vaultPath}</Value>
        </Row>
      ) : null}
      <Row
        label={t('trading.settings.vault.mode')}
        help={
          reason ? (
            <span data-testid="vault-mode-reason">{reason}</span>
          ) : (
            t('trading.settings.vault.mode.help')
          )
        }
      >
        <span ref={cardRef} title={reason ?? undefined} className="contents">
          <Segmented
            label={t('trading.settings.vault.mode')}
            value={pendingMode ?? unlockMode}
            disabled={reason !== null || write.isPending}
            options={[
              { value: 'auto', label: t('trading.sheet.setup.mode.auto') },
              { value: 'manual', label: t('trading.sheet.setup.mode.manual') },
            ]}
            onChange={(mode) => setPendingMode(mode === unlockMode ? null : mode)}
          />
        </span>
      </Row>
      {pendingMode ? (
        <Row label={t('trading.sheet.password')} htmlFor={id}>
          <input
            id={id}
            className="mac-input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => {
              // First Escape drops the pending change, not the whole sheet.
              if (e.key !== 'Escape') return
              e.preventDefault()
              e.stopPropagation()
              setPendingMode(null)
              setPassword('')
              keepFocus(cardRef.current)
            }}
          />
          <Button
            variant="primary"
            disabled={!password || write.isPending}
            onClick={() =>
              write.mutate(
                { method: 'wallet.setUnlockMode', params: { mode: pendingMode, password } },
                {
                  onSuccess: () => {
                    setPendingMode(null)
                    setPassword('')
                    keepFocus(cardRef.current)
                    toast.success(t('trading.settings.saved'), { id: 'stg-vault' })
                  },
                  onError: (err) => {
                    if (isOperatorRefusal(err)) {
                      setPendingMode(null)
                      setPassword('')
                      keepFocus(cardRef.current)
                      onRefused()
                      toast.error(t('settings.trading.readOnly.refused'), { id: 'stg-vault' })
                      return
                    }
                    toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, {
                      id: 'stg-vault',
                    })
                  },
                },
              )
            }
          >
            {t('settings.save')}
          </Button>
        </Row>
      ) : null}
    </Card>
  )
}

function NumberRow({
  label,
  help,
  value,
  unit,
  step,
  lockReason,
  saving,
  onSave,
  allowEmpty,
}: {
  label: string
  help: string
  value: number | null | undefined
  unit: string
  step?: number
  lockReason: string | null
  saving: boolean
  /** Resolves false when the write failed: the field then shows the saved value again. */
  onSave: (value: number | null) => Promise<boolean>
  allowEmpty?: boolean
}) {
  const id = useId()
  const savedText = value === null || value === undefined ? '' : String(value)
  const [draft, setDraft] = useState(savedText)
  const parsed = draft.trim() === '' ? null : Number(draft)
  const invalid = parsed !== null && (!Number.isFinite(parsed) || parsed < 0)
  const dirty = draft.trim() !== savedText
  return (
    <Row
      label={label}
      htmlFor={id}
      help={invalid ? <span className="stg-error">{t('trading.settings.invalid')}</span> : help}
    >
      <span className="trd-stg__unit">
        <input
          id={id}
          className="mac-input"
          data-short="true"
          inputMode="decimal"
          step={step}
          value={draft}
          disabled={lockReason !== null}
          title={lockReason ?? undefined}
          data-invalid={invalid ? 'true' : undefined}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            if (!dirty || invalid) return
            // Blank where a number is required is not a value: show the saved one.
            if (parsed === null && !allowEmpty) {
              setDraft(savedText)
              return
            }
            const sent = savedText
            void onSave(parsed).then((ok) => {
              if (!ok) setDraft(sent)
            })
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            // First Escape puts the saved value back; the next closes the sheet.
            else if (e.key === 'Escape' && dirty) {
              e.preventDefault()
              e.stopPropagation()
              setDraft(savedText)
            }
          }}
        />
        {unit}
        {saving ? <LoaderCircle className="stg-spin size-3" strokeWidth={2} aria-hidden /> : null}
      </span>
    </Row>
  )
}

function LimitsCard({
  cfg,
  lockReason,
  saving,
  onSave,
}: {
  cfg: TradingConfig
  lockReason: string | null
  saving: boolean
  onSave: (patch: Record<string, unknown>) => Promise<boolean>
}) {
  return (
    <Card title={t('trading.settings.limits')} blurb={t('trading.settings.limits.blurb')}>
      <NumberRow
        label={t('trading.settings.threshold')}
        help={t('trading.settings.threshold.help')}
        value={cfg.approval_threshold_usd ?? 100}
        unit="USD"
        lockReason={lockReason}
        saving={saving}
        onSave={(v) => onSave({ approval_threshold_usd: v })}
      />
      <NumberRow
        label={t('trading.settings.dailyCap')}
        help={t('trading.settings.dailyCap.help')}
        value={cfg.daily_cap_usd ?? 1000}
        unit="USD"
        lockReason={lockReason}
        saving={saving}
        onSave={(v) => onSave({ daily_cap_usd: v })}
      />
      <NumberRow
        label={t('trading.settings.ttl')}
        help={t('trading.settings.ttl.help')}
        value={Math.round((cfg.approval_ttl_seconds ?? 900) / 60)}
        unit="min"
        lockReason={lockReason}
        saving={saving}
        onSave={(v) => onSave({ approval_ttl_seconds: Math.round((v ?? 15) * 60) })}
      />
      <NumberRow
        label={t('trading.settings.slippage')}
        help={t('trading.settings.slippage.help')}
        value={cfg.default_slippage_pct ?? null}
        unit="%"
        step={0.1}
        lockReason={lockReason}
        saving={saving}
        allowEmpty
        onSave={(v) => onSave({ default_slippage_pct: v })}
      />
    </Card>
  )
}

function RpcRow({
  name,
  chainId,
  healthy,
  value,
  placeholder,
  lockReason,
  saving,
  onSave,
}: {
  name: string
  chainId: number
  healthy: boolean | null
  value: string
  placeholder: string
  lockReason: string | null
  saving: boolean
  /** Resolves false when the write failed: the field then shows the saved URL again. */
  onSave: (url: string) => Promise<boolean>
}) {
  const id = useId()
  const [draft, setDraft] = useState(value)
  const dirty = draft.trim() !== value
  return (
    <Row
      label={`${name} · ${chainId}`}
      htmlFor={id}
      help={
        <span className="flex items-center gap-2">
          {t('trading.settings.rpc.help')}
          <Pill tone={healthy === true ? 'ok' : healthy === false ? 'danger' : undefined}>
            {healthy === true
              ? t('trading.settings.chain.healthy')
              : healthy === false
                ? t('trading.settings.chain.down')
                : t('trading.settings.chain.unknown')}
          </Pill>
        </span>
      }
      stack
    >
      <span className="flex w-full items-center gap-2">
        <input
          id={id}
          className="mac-input"
          data-mono="true"
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={draft}
          disabled={lockReason !== null}
          title={lockReason ?? undefined}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && dirty) {
              e.preventDefault()
              e.stopPropagation()
              setDraft(value)
            }
          }}
        />
        <Button
          disabled={!dirty || saving || lockReason !== null}
          title={lockReason ?? undefined}
          onClick={(e) => {
            const sent = value
            const sheet = sheetOf(e)
            void onSave(draft.trim()).then((ok) => {
              if (!ok) setDraft(sent)
              keepFocus(sheet, () => document.getElementById(id))
            })
          }}
        >
          {t('settings.save')}
        </Button>
      </span>
    </Row>
  )
}
