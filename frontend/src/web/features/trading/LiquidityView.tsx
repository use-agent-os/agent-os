import { Droplets } from 'lucide-react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webTrading'
import { errorText, formatAmountCompact } from '@/views/trading/logic'
import { formatRate, formatShare, mcapRange, positionTag } from '@/views/trading/liquidity-logic'
import type { LpPosition } from '@/views/trading/types'
import { Badge } from '@/web/components/ui/badge'
import { Card, CardHeader } from '@/web/components/ui/card'
import { EmptyState, ErrorState, LoadingRegion, Skeleton } from '@/web/components/ui/states'
import { useLpBook } from './data'
import { Amount, ChainChip, Money, StatusBadge } from './parts'
import { clampSym, clampText } from './safety'

// Liquidity: the engine's book of Uniswap and Aerodrome positions for the
// shown wallets, as a list. The book is a cached read; this page never asks
// for a fresh chain scan, and collecting or removing stays in the desktop app.

function rangeText(p: LpPosition): string | null {
  const mcap = mcapRange(p)
  if (mcap) return mcap
  const lower = p.range?.priceLower
  const upper = p.range?.priceUpper
  if (lower == null || upper == null) return null
  return `${formatRate(lower)} – ${formatRate(upper)} ${clampSym(p.quote.symbol)}`
}

function Row({ p }: { p: LpPosition }) {
  const base = clampSym(p.token.symbol)
  const quote = clampSym(p.quote.symbol)
  const tag = positionTag(p)
  const share = formatShare(p.share)
  const range = rangeText(p)
  return (
    <tr data-testid="trd-lp">
      <td data-col="asset">
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate font-medium">
            {base}/{quote}
          </span>
          <span className="trd-token__sub">
            <ChainChip chainId={p.chain.id} />
            <span className="truncate">{clampText(p.protocol.name, 24)}</span>
            {p.pool.feePct ? <span>· {clampText(p.pool.feePct, 10)}</span> : null}
            {tag ? <span className="trd-mono">{clampText(tag, 16)}</span> : null}
          </span>
        </span>
      </td>
      <td data-col="range">
        <span className="flex min-w-0 flex-col items-start gap-1">
          <span className="flex flex-wrap items-center gap-1">
            <StatusBadge status={p.status} />
            {p.staked ? <Badge tone="accent">{t('webTrading.lpStaked')}</Badge> : null}
          </span>
          {range ? <span className="trd-note trd-num">{range}</span> : null}
          {share ? (
            <span className="trd-note trd-num">{t('webTrading.lpShare', { share })}</span>
          ) : null}
        </span>
      </td>
      <td data-col="amount" className="trd-cell-num">
        <span className="flex flex-col items-end">
          <Amount text={`${formatAmountCompact(p.principal.base.human)} ${base}`} />
          <Amount
            className="trd-note"
            text={`${formatAmountCompact(p.principal.quote.human)} ${quote}`}
          />
        </span>
      </td>
      <td data-col="value" className="trd-cell-num">
        <Money value={p.valueUsd} />
      </td>
      <td data-col="fees" className="trd-cell-num">
        <span className="flex flex-col items-end">
          {p.fees ? <Money value={p.fees.usd} /> : <span className="trd-note">—</span>}
          {p.rewards ? (
            <Amount
              className="trd-note"
              text={`${formatAmountCompact(p.rewards.amount.human)} ${clampSym(p.rewards.token.symbol)}`}
            />
          ) : null}
        </span>
      </td>
    </tr>
  )
}

export function LiquidityView({ wallet }: { wallet?: string }) {
  const book = useLpBook(wallet, true)

  if (book.isError && !book.data)
    return (
      <ErrorState
        title={t('webTrading.liquidityFailed')}
        message={errorText(book.error)}
        onRetry={() => void book.refetch()}
      />
    )
  if (!book.data || (!book.data.ready && book.data.positions.length === 0))
    return (
      <LoadingRegion label={t('webTrading.liquidityReading')} className="flex flex-col gap-4">
        <Skeleton className="h-24 rounded-card" />
        <Skeleton className="h-48 rounded-card" />
      </LoadingRegion>
    )

  const { positions, totals } = book.data
  if (positions.length === 0)
    return (
      <EmptyState
        icon={<Droplets />}
        title={t('webTrading.liquidityEmpty')}
        description={t('webTrading.liquidityEmptyBody')}
      />
    )

  return (
    <div className="flex flex-col gap-4" data-testid="trd-liquidity">
      <Card asChild>
        <dl className="trd-pnl">
          <div>
            <dt>{t('webTrading.colValue')}</dt>
            <dd>
              <Money value={totals.valueUsd} />
            </dd>
          </div>
          <div>
            <dt>{t('webTrading.colFees')}</dt>
            <dd>
              <Money value={totals.feesUsd} />
            </dd>
          </div>
          {totals.rewardsUsd ? (
            <div>
              <dt>{t('webTrading.lpRewards')}</dt>
              <dd>
                <Money value={totals.rewardsUsd} />
              </dd>
            </div>
          ) : null}
          <div>
            <dt>{t('webTrading.lpPositions')}</dt>
            <dd className="trd-num">{totals.count}</dd>
          </div>
        </dl>
      </Card>
      {totals.outOfRange > 0 ? (
        <p className="trd-callout">{tPlural('webTrading.outOfRange', totals.outOfRange)}</p>
      ) : null}
      {book.data.partialScan ? (
        <p className="trd-callout">{t('webTrading.liquidityPartial')}</p>
      ) : null}
      <Card asChild padding="none" className="trd-table-wrap">
        <section aria-label={t('webTrading.viewLiquidity')}>
          <CardHeader title={t('webTrading.viewLiquidity')} className="px-4 pt-4" />
          <p className="trd-note px-4 pb-2">{t('webTrading.liquidityActionsNote')}</p>
          <table className="trd-table" aria-label={t('webTrading.viewLiquidity')}>
            <thead>
              <tr>
                <th scope="col" data-col="asset">
                  {t('webTrading.colPosition')}
                </th>
                <th scope="col" data-col="range">
                  {t('webTrading.colRange')}
                </th>
                <th scope="col" data-col="amount" className="trd-cell-num">
                  {t('webTrading.colAmount')}
                </th>
                <th scope="col" data-col="value" className="trd-cell-num">
                  {t('webTrading.colValue')}
                </th>
                <th scope="col" data-col="fees" className="trd-cell-num">
                  {t('webTrading.colFees')}
                </th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <Row key={p.id} p={p} />
              ))}
            </tbody>
          </table>
        </section>
      </Card>
    </div>
  )
}
