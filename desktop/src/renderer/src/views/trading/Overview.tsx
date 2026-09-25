import { RefreshCw, TrendingDown, TrendingUp } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { shortAge } from '~/lib/relative-time'
import { allocationSegments, formatPct, formatUsd, pnlTone } from './logic'
import { Money, Spinner, useCountUp } from './parts'
import { ProviderMark, providerMark } from './ProviderMark'
import { ProviderMenu } from './ProviderMenu'
import {
  providerLabel,
  type Holding,
  type ProviderId,
  type ProviderStatus,
  type Totals,
} from './types'

/**
 * The instrument head of the desk: one panel carrying whose wallet this is,
 * the value in the display face, today's move as a toned chip, four figures
 * on their own tiles, and the allocation meter. Everything here is a total of
 * what the table below lists; nothing is computed twice.
 */
export function Overview({
  totals,
  holdings,
  syncing,
  lastSyncAt,
  now,
  onSync,
  loading,
  provider,
  providers,
  switchingProvider,
  onSwitchProvider,
  onOpenSettings,
  entering,
  head,
  unpricedCount = 0,
}: {
  totals: Totals
  holdings: Holding[]
  syncing: boolean
  lastSyncAt: number | null
  now: number
  onSync: () => void
  loading: boolean
  /** Who routes swaps right now, as a pill beside the sync state that switches it. */
  provider?: ProviderId
  /** Per-provider facts from trading.status; the pill's menu explains each choice. */
  providers?: readonly ProviderStatus[]
  switchingProvider?: boolean
  onSwitchProvider?: (id: ProviderId) => void
  /** Settings › Trading, where a Uniswap key is added. */
  onOpenSettings: () => void
  /** The mode switch is playing: the value counts up on the same clock. */
  entering?: boolean
  /** Whose value this is: the wallet head, above the figure. */
  head?: ReactNode
  /** Positions the totals could not price (`portfolio.unpricedCount`). */
  unpricedCount?: number
}) {
  // The hero is coloured by the book's own result — banked plus open PnL —
  // never by the day's price move, which is only the chip's business.
  const tone = pnlTone((totals.realizedUsd ?? 0) + (totals.unrealizedUsd ?? 0))
  const deltaTone = pnlTone(totals.change24hUsd)
  const segments = allocationSegments(holdings)
  const Arrow = deltaTone === 'down' ? TrendingDown : TrendingUp
  const { counting, attach } = useCountUp(totals.valueUsd, Boolean(entering) && !loading)

  return (
    <section className="trd-hero" data-tone={tone} aria-label={t('trading.overview.value')}>
      {head}

      <div className="trd-hero__figure">
        <b data-testid="portfolio-value">
          {loading ? (
            <span className="trd-skel" style={{ width: 190, height: 34 }} />
          ) : counting ? (
            <span className="trd-num" data-testid="portfolio-value-counting" ref={attach}>
              {formatUsd(0)}
            </span>
          ) : (
            <Money value={totals.valueUsd} />
          )}
        </b>
        {!loading && totals.change24hUsd !== null ? (
          <span
            className="trd-delta"
            data-tone={deltaTone}
            data-testid="portfolio-delta"
            aria-label={t('trading.overview.move24h')}
            title={`${t('trading.overview.move24h')}: ${formatUsd(totals.change24hUsd, { signed: true })} ${t('trading.overview.today')}`}
          >
            {deltaTone !== 'flat' ? (
              <Arrow className="size-3.5" strokeWidth={2.25} aria-hidden />
            ) : null}
            <Money value={totals.change24hUsd} signed cell />
            <em className="trd-num">{formatPct(totals.change24hPct, { signed: true })}</em>
            <small>{t('trading.overview.today')}</small>
          </span>
        ) : null}
        {!loading && unpricedCount > 0 ? (
          <span className="trd-delta" data-tone="flat" data-testid="portfolio-unpriced">
            <em className="trd-num">{unpricedCount}</em>
            <small>{t('trading.overview.unpriced')}</small>
          </span>
        ) : null}

        {/* Where orders route and how fresh the figures are: facts about the
            desk, not about the wallet, so they sit with the value rather than
            in the identity line above it. The route is also switched here,
            through the same menu as the desk chat's route seat. */}
        <div className="trd-hero__tools">
          {provider ? (
            <ProviderMenu
              className="trd-venue app-no-drag"
              testId="provider-pill"
              provider={provider}
              providers={providers ?? []}
              switching={Boolean(switchingProvider)}
              onSwitch={onSwitchProvider}
              onOpenSettings={onOpenSettings}
            >
              {providerMark(provider) ? (
                <ProviderMark id={provider} size={13} />
              ) : (
                <i aria-hidden />
              )}
              {providerLabel(provider)}
            </ProviderMenu>
          ) : null}
          <span className="trd-hero__sync" data-live={syncing ? 'true' : undefined}>
            {syncing ? (
              <>
                <Spinner className="size-3" />
                {t('trading.syncing')}
              </>
            ) : (
              <>
                {t('trading.lastSync')}{' '}
                {lastSyncAt ? shortAge(lastSyncAt, now) : t('trading.never')}
              </>
            )}
          </span>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('trading.sync')}
            title={t('trading.sync')}
            disabled={syncing}
            onClick={onSync}
          >
            <RefreshCw
              className="trd-hero__spin size-3.5 text-muted-foreground"
              strokeWidth={1.75}
              aria-hidden
            />
          </Button>
        </div>
      </div>

      <div className="trd-hero__stats">
        <Stat
          label={t('trading.overview.unrealized')}
          value={totals.unrealizedUsd}
          signed
          toned
          pct={totals.costUsd > 0 ? (totals.unrealizedUsd / totals.costUsd) * 100 : null}
        />
        <Stat label={t('trading.overview.realized')} value={totals.realizedUsd} signed toned />
        <Stat label={t('trading.overview.cost')} value={totals.costUsd} />
        <Stat label={t('trading.overview.gas')} value={totals.gasUsd} />
      </div>

      {segments.length > 0 ? (
        <div className="trd-alloc" aria-label={t('trading.overview.allocation')}>
          <div className="trd-alloc__bar" aria-hidden>
            {segments.map((s, i) => (
              <span
                key={s.symbol}
                style={{ ['--i' as string]: i, flexBasis: `${s.pct}%` }}
                data-other={s.symbol === 'other' ? 'true' : undefined}
                title={`${s.symbol} ${formatPct(s.pct)}`}
              />
            ))}
          </div>
          <div className="trd-alloc__legend">
            {segments.map((s, i) => (
              <span key={s.symbol} style={{ ['--i' as string]: i }}>
                <i data-other={s.symbol === 'other' ? 'true' : undefined} aria-hidden />
                {s.symbol === 'other' ? t('trading.overview.other') : s.symbol}
                <b>{formatPct(s.pct)}</b>
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  )
}

/** One figure on its own tile: a micro label, the number, its rate beside it. */
function Stat({
  label,
  value,
  signed,
  toned,
  pct,
}: {
  label: string
  value: number
  signed?: boolean
  toned?: boolean
  pct?: number | null
}) {
  return (
    <div className="trd-stat" title={formatUsd(value, { signed })}>
      <span className="trd-stat__label">{label}</span>
      <span className="trd-stat__value">
        <Money value={value} signed={signed} toned={toned} cell />
        {pct !== undefined && pct !== null && Number.isFinite(pct) ? (
          <small className="trd-num" data-tone={pnlTone(pct)}>
            {formatPct(pct, { signed: true })}
          </small>
        ) : null}
      </span>
    </div>
  )
}
