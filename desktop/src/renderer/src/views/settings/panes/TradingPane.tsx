import { useMutation } from '@tanstack/react-query'
import { AlertTriangle, Check, ExternalLink, Eye, EyeOff, LoaderCircle } from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
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
import { Card, Choices, Head, Notice, Pill, Row, Segmented, Stat, Stats, Value } from '../parts'
import { useConfigSnapshot, withRevision } from '../use-snapshot'

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
  const blocked = Boolean(snapshot.writeBlocked)
  const status = useTradingStatus()
  const vault = useWalletStatus()
  const provider: ProviderId = isProviderId(cfg.provider)
    ? cfg.provider
    : (status.data?.provider ?? DEFAULT_PROVIDER)

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
    onError: (err) =>
      toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, { id: 'stg-trading-err' }),
  })

  const enabled = cfg.enabled !== false
  const vaultState = !vault.data?.initialized ? 'none' : vault.data.unlocked ? 'unlocked' : 'locked'
  const providerName = PROVIDERS.find((p) => p.id === provider)?.label ?? provider

  return (
    <>
      {head(
        <label className="trd-switch" title={t('trading.settings.enabled.help')}>
          <span>{enabled ? t('settings.trading.on') : t('settings.trading.off')}</span>
          <Switch
            checked={enabled}
            disabled={blocked || save.isPending}
            aria-label={t('trading.settings.enabled')}
            onCheckedChange={(next) => save.mutate({ enabled: next })}
          />
        </label>,
      )}

      {snapshot.writeBlocked ? <Notice tone="danger">{t('settings.writeBlocked')}</Notice> : null}
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
        <Choices<ProviderId>
          label={t('trading.settings.provider')}
          value={provider}
          disabled={blocked || save.isPending}
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
            if (next !== provider) save.mutate({ provider: next })
          }}
        />
        <AggregatorRow status={status.data?.providers?.find((p) => p.id === 'aggregator')} />
      </Card>

      <KeyCard
        cfg={cfg}
        configured={Boolean(status.data?.apiKeyConfigured)}
        blocked={blocked}
        saving={save.isPending}
        idle={provider !== 'uniswap'}
        onSave={(k) => save.mutate({ uniswap_api_key: k })}
      />

      <VaultCard
        unlockMode={vault.data?.unlockMode ?? cfg.unlock_mode ?? 'auto'}
        initialized={Boolean(vault.data?.initialized)}
        vaultPath={vault.data?.vaultPath ?? ''}
        blocked={blocked}
      />

      <LimitsCard
        cfg={cfg}
        blocked={blocked}
        saving={save.isPending}
        onSave={(patch) => save.mutate(patch)}
      />

      <Card title={t('trading.settings.chains')} blurb={t('trading.settings.chains.blurb')}>
        {(status.data?.chains ?? []).map((c) => (
          <RpcRow
            key={c.chainId}
            name={c.name}
            chainId={c.chainId}
            healthy={c.healthy}
            value={cfg.rpc_urls?.[String(c.chainId)] ?? ''}
            placeholder={c.rpcUrl}
            blocked={blocked}
            saving={save.isPending}
            onSave={(url) =>
              save.mutate({ rpc_urls: { ...(cfg.rpc_urls ?? {}), [String(c.chainId)]: url } })
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
  blocked,
  saving,
  idle,
  onSave,
}: {
  cfg: TradingConfig
  configured: boolean
  blocked: boolean
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
            disabled={!key.trim() || saving || blocked}
            onClick={() => {
              onSave(key.trim())
              setKey('')
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
            disabled={blocked}
            onChange={(e) => setKey(e.target.value)}
          />
          <Button
            variant="ghost"
            size="icon"
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
          disabled={probe.isPending || (!key.trim() && !hasStored)}
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
  blocked,
}: {
  unlockMode: UnlockMode
  initialized: boolean
  vaultPath: string
  blocked: boolean
}) {
  const id = useId()
  const [pendingMode, setPendingMode] = useState<UnlockMode | null>(null)
  const [password, setPassword] = useState('')
  const write = useWalletMutation()
  return (
    <Card title={t('trading.settings.vault')} blurb={t('trading.settings.vault.blurb')}>
      {vaultPath ? (
        <Row label={t('trading.settings.vault.path')}>
          <Value title={vaultPath}>{vaultPath}</Value>
        </Row>
      ) : null}
      <Row label={t('trading.settings.vault.mode')} help={t('trading.settings.vault.mode.help')}>
        <Segmented
          label={t('trading.settings.vault.mode')}
          value={pendingMode ?? unlockMode}
          disabled={blocked || !initialized || write.isPending}
          options={[
            { value: 'auto', label: t('trading.sheet.setup.mode.auto') },
            { value: 'manual', label: t('trading.sheet.setup.mode.manual') },
          ]}
          onChange={(mode) => setPendingMode(mode === unlockMode ? null : mode)}
        />
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
                    toast.success(t('trading.settings.saved'), { id: 'stg-vault' })
                  },
                  onError: (err) =>
                    toast.error(`${t('settings.saveFailed')}: ${errorText(err)}`, {
                      id: 'stg-vault',
                    }),
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
  blocked,
  saving,
  onSave,
  allowEmpty,
}: {
  label: string
  help: string
  value: number | null | undefined
  unit: string
  step?: number
  blocked: boolean
  saving: boolean
  onSave: (value: number | null) => void
  allowEmpty?: boolean
}) {
  const id = useId()
  const [draft, setDraft] = useState(value === null || value === undefined ? '' : String(value))
  const parsed = draft.trim() === '' ? null : Number(draft)
  const invalid = parsed !== null && (!Number.isFinite(parsed) || parsed < 0)
  const dirty = draft.trim() !== (value === null || value === undefined ? '' : String(value))
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
          disabled={blocked}
          data-invalid={invalid ? 'true' : undefined}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            if (!dirty || invalid) return
            if (parsed === null && !allowEmpty) return
            onSave(parsed)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
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
  blocked,
  saving,
  onSave,
}: {
  cfg: TradingConfig
  blocked: boolean
  saving: boolean
  onSave: (patch: Record<string, unknown>) => void
}) {
  return (
    <Card title={t('trading.settings.limits')} blurb={t('trading.settings.limits.blurb')}>
      <NumberRow
        label={t('trading.settings.threshold')}
        help={t('trading.settings.threshold.help')}
        value={cfg.approval_threshold_usd ?? 100}
        unit="USD"
        blocked={blocked}
        saving={saving}
        onSave={(v) => onSave({ approval_threshold_usd: v })}
      />
      <NumberRow
        label={t('trading.settings.dailyCap')}
        help={t('trading.settings.dailyCap.help')}
        value={cfg.daily_cap_usd ?? 1000}
        unit="USD"
        blocked={blocked}
        saving={saving}
        onSave={(v) => onSave({ daily_cap_usd: v })}
      />
      <NumberRow
        label={t('trading.settings.ttl')}
        help={t('trading.settings.ttl.help')}
        value={Math.round((cfg.approval_ttl_seconds ?? 900) / 60)}
        unit="min"
        blocked={blocked}
        saving={saving}
        onSave={(v) => onSave({ approval_ttl_seconds: Math.round((v ?? 15) * 60) })}
      />
      <NumberRow
        label={t('trading.settings.slippage')}
        help={t('trading.settings.slippage.help')}
        value={cfg.default_slippage_pct ?? null}
        unit="%"
        step={0.1}
        blocked={blocked}
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
  blocked,
  saving,
  onSave,
}: {
  name: string
  chainId: number
  healthy: boolean | null
  value: string
  placeholder: string
  blocked: boolean
  saving: boolean
  onSave: (url: string) => void
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
          disabled={blocked}
          onChange={(e) => setDraft(e.target.value)}
        />
        <Button disabled={!dirty || saving || blocked} onClick={() => onSave(draft.trim())}>
          {t('settings.save')}
        </Button>
      </span>
    </Row>
  )
}
