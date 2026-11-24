import { ChartLine, Coins } from 'lucide-react'
import { useState } from 'react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webTrading'
import {
  formatAmount,
  formatPct,
  formatPrice,
  pnlTone,
  sortHoldings,
  splitDust,
} from '@/views/trading/logic'
import type { ChainRead, Holding, Portfolio } from '@/views/trading/types'
import { Button } from '@/web/components/ui/button'
import { Card } from '@/web/components/ui/card'
import { EmptyState } from '@/web/components/ui/states'
import { Amount, ChainReadWarning, Money, SyncNote, TokenCell } from './parts'
import { PriceChart } from './PriceChart'
import { clampSym } from './safety'

// Holdings: every priced or unpriced position the engine holds for the shown
// wallets, by value; dust folds away behind a switch. A row's chart button
// opens that token's price line above the table.

function holdingKey(h: Holding): string {
  return `${h.chainId}:${h.token.address.toLowerCase()}:${h.wallet ?? ''}`
}

function Row({ h, onChart }: { h: Holding; onChart: (h: Holding) => void }) {
  const symbol = clampSym(h.token.symbol)
  return (
    <tr>
      <td data-col="asset">
        <TokenCell
          symbol={h.token.symbol}
          name={h.token.name}
          address={h.token.address}
          chainId={h.chainId}
          verified={h.token.verified}
        />
      </td>
      <td data-col="amount" className="trd-cell-num">
        <Amount text={formatAmount(h.amount)} />
      </td>
      <td data-col="price" className="trd-cell-num">
        <span className="trd-num">{formatPrice(h.priceUsd)}</span>
      </td>
      <td data-col="value" className="trd-cell-num">
        <Money value={h.valueUsd} />
      </td>
      <td data-col="change" className="trd-cell-num">
        <span className="trd-num" data-tone={pnlTone(h.change24hPct)}>
          {formatPct(h.change24hPct, { signed: true })}
        </span>
      </td>
      <td data-col="pnl" className="trd-cell-num">
        <Money value={h.unrealizedUsd} signed toned />
      </td>
      <td data-col="chart" className="trd-cell-action">
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => onChart(h)}
          aria-label={t('webTrading.chartFor', { symbol })}
          title={t('webTrading.chartFor', { symbol })}
        >
          <ChartLine aria-hidden />
        </Button>
      </td>
    </tr>
  )
}

export function HoldingsView({
  portfolio,
  syncing,
  problems,
}: {
  portfolio: Portfolio
  syncing: boolean
  problems: readonly ChainRead[]
}) {
  const [showDust, setShowDust] = useState(false)
  const [charted, setCharted] = useState<Holding | null>(null)
  const { kept, dust } = splitDust(sortHoldings(portfolio.holdings, 'value'))
  const rows = showDust ? [...kept, ...dust] : kept

  // An empty list is only final once the chain was read: while a sync runs
  // or a read failed, it says so instead of "no holdings".
  if (portfolio.holdings.length === 0) {
    const unread = syncing || problems.length > 0
    return (
      <div className="flex flex-col gap-4">
        <ChainReadWarning problems={problems} />
        <SyncNote syncing={syncing} />
        <EmptyState
          icon={<Coins />}
          title={t(unread ? 'webTrading.holdingsUnread' : 'webTrading.holdingsEmpty')}
          description={t(unread ? 'webTrading.holdingsUnreadBody' : 'webTrading.holdingsEmptyBody')}
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4" data-testid="trd-holdings">
      <ChainReadWarning problems={problems} />
      <SyncNote syncing={syncing} />
      {charted ? (
        <PriceChart
          key={holdingKey(charted)}
          chainId={charted.chainId}
          token={charted.token.address}
          symbol={charted.token.symbol}
          onClose={() => setCharted(null)}
        />
      ) : null}
      <Card padding="none" className="trd-table-wrap">
        <table className="trd-table" aria-label={t('webTrading.viewHoldings')}>
          <thead>
            <tr>
              <th scope="col" data-col="asset">
                {t('webTrading.colAsset')}
              </th>
              <th scope="col" data-col="amount" className="trd-cell-num">
                {t('webTrading.colAmount')}
              </th>
              <th scope="col" data-col="price" className="trd-cell-num">
                {t('webTrading.colPrice')}
              </th>
              <th scope="col" data-col="value" className="trd-cell-num">
                {t('webTrading.colValue')}
              </th>
              <th scope="col" data-col="change" className="trd-cell-num">
                {t('webTrading.col24h')}
              </th>
              <th scope="col" data-col="pnl" className="trd-cell-num">
                {t('webTrading.colPnl')}
              </th>
              <th scope="col" data-col="chart">
                <span className="sr-only">{t('webTrading.colChart')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((h) => (
              <Row key={holdingKey(h)} h={h} onChart={setCharted} />
            ))}
          </tbody>
        </table>
      </Card>
      {dust.length > 0 ? (
        <Button
          variant="ghost"
          size="sm"
          className="self-start"
          aria-expanded={showDust}
          onClick={() => setShowDust((v) => !v)}
        >
          {showDust
            ? t('webTrading.dustHide')
            : tPlural('webTrading.dustShow', dust.length, { count: dust.length })}
        </Button>
      ) : null}
    </div>
  )
}
