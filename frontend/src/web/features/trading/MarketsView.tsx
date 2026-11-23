import { Search, Store } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { t } from '@/i18n'
import '@/i18n/en/webTrading'
import { chainName, errorText, formatPct, formatPrice, pnlTone } from '@/views/trading/logic'
import {
  dexLabel,
  feeLabel,
  formatPoolPrice,
  formatUsdShort,
  rowPair,
  showsCounterpartyName,
} from '@/views/trading/markets-logic'
import type { MarketsParams, MarketsPool, MarketsSide } from '@/views/trading/types'
import { Button } from '@/web/components/ui/button'
import { Card, CardHeader } from '@/web/components/ui/card'
import { Input } from '@/web/components/ui/input'
import { EmptyState, ErrorState, Skeleton } from '@/web/components/ui/states'
import { useMarkets } from './data'
import { TokenCell } from './parts'
import { PriceChart } from './PriceChart'
import { clampSym, clampText } from './safety'

// Markets: every pool a token trades in on one network, as the engine reads
// them from its price source, with the token's price line on top. Rows link
// nowhere and offer no swap: the desktop app trades, this page looks.

const TARGET_MAX = 64

function PoolRows({ pools, symbol }: { pools: MarketsPool[]; symbol: string }) {
  return (
    <>
      {pools.map((p) => {
        const [a, b] = rowPair(p, symbol)
        const fee = feeLabel(p.feePct)
        return (
          <tr key={`${p.side}:${p.poolAddress}`} data-testid="trd-pool">
            <td data-col="asset">
              <span className="flex min-w-0 flex-col">
                <span className="truncate font-medium">
                  {clampSym(a)}/{clampSym(b)}
                </span>
                {showsCounterpartyName(p, symbol) ? (
                  <span className="trd-note truncate">{clampText(p.counterparty.name, 28)}</span>
                ) : null}
              </span>
            </td>
            <td data-col="dex">
              <span className="truncate">{clampText(dexLabel(p.dex), 24)}</span>
              {fee ? <span className="trd-note"> · {fee}</span> : null}
            </td>
            <td data-col="tvl" className="trd-cell-num">
              <span className="trd-num">{formatUsdShort(p.tvlUsd)}</span>
            </td>
            <td data-col="price" className="trd-cell-num">
              <span className="trd-num">{formatPoolPrice(p.priceInToken)}</span>
            </td>
            <td data-col="volume" className="trd-cell-num">
              <span className="trd-num">{formatUsdShort(p.volume24hUsd)}</span>
            </td>
            <td data-col="change" className="trd-cell-num">
              <span className="trd-num" data-tone={pnlTone(p.change24hPct)}>
                {formatPct(p.change24hPct, { signed: true })}
              </span>
            </td>
          </tr>
        )
      })}
    </>
  )
}

function PoolTable({
  side,
  pools,
  symbol,
}: {
  side: MarketsSide
  pools: MarketsPool[]
  symbol: string
}) {
  const title =
    side === 'quote'
      ? t('webTrading.marketsQuote', { symbol })
      : t('webTrading.marketsBase', { symbol })
  return (
    <Card asChild padding="none" className="trd-table-wrap">
      <section aria-label={title}>
        <CardHeader title={title} className="px-4 pt-4" />
        <table className="trd-table" aria-label={title}>
          <thead>
            <tr>
              <th scope="col" data-col="asset">
                {t('webTrading.colPair')}
              </th>
              <th scope="col" data-col="dex">
                {t('webTrading.colDex')}
              </th>
              <th scope="col" data-col="tvl" className="trd-cell-num">
                {t('webTrading.colTvl')}
              </th>
              <th scope="col" data-col="price" className="trd-cell-num">
                {t('webTrading.colPrice')}
              </th>
              <th scope="col" data-col="volume" className="trd-cell-num">
                {t('webTrading.colVolume')}
              </th>
              <th scope="col" data-col="change" className="trd-cell-num">
                {t('webTrading.col24h')}
              </th>
            </tr>
          </thead>
          <tbody>
            <PoolRows pools={pools} symbol={symbol} />
          </tbody>
        </table>
      </section>
    </Card>
  )
}

export function MarketsView({ chains }: { chains: readonly number[] }) {
  const defaultChain = chains[0] ?? 8453
  const [draft, setDraft] = useState('')
  const [chainId, setChainId] = useState(defaultChain)
  const [params, setParams] = useState<MarketsParams | null>(null)
  const markets = useMarkets(params)
  const inputId = useId()
  const chainPicker = useId()

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const target = draft.trim().slice(0, TARGET_MAX)
    if (target) setParams({ target, chainId })
  }

  const data = markets.data
  const symbol = data ? clampSym(data.token.symbol) : ''
  const empty = data && data.sections.quote.length === 0 && data.sections.base.length === 0

  return (
    <div className="flex flex-col gap-4" data-testid="trd-markets">
      <form className="trd-search" onSubmit={submit} role="search">
        <label htmlFor={inputId} className="sr-only">
          {t('webTrading.marketsLabel')}
        </label>
        <Input
          id={inputId}
          value={draft}
          maxLength={TARGET_MAX}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={t('webTrading.marketsPlaceholder')}
          autoComplete="off"
          spellCheck={false}
          className="trd-search__input"
        />
        {chains.length > 1 ? (
          <label htmlFor={chainPicker}>
            <span className="sr-only">{t('webTrading.marketsNetwork')}</span>
            <select
              id={chainPicker}
              className="trd-select"
              value={chainId}
              onChange={(e) => setChainId(Number(e.target.value))}
            >
              {chains.map((c) => (
                <option key={c} value={c}>
                  {chainName(c)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <Button type="submit" variant="primary" disabled={!draft.trim()}>
          <Search aria-hidden />
          {t('webTrading.marketsSearch')}
        </Button>
      </form>

      {!params ? (
        <EmptyState
          size="sm"
          icon={<Store />}
          title={t('webTrading.viewMarkets')}
          description={t('webTrading.marketsIntro')}
        />
      ) : markets.isError ? (
        <ErrorState
          title={t('webTrading.marketsFailed')}
          message={errorText(markets.error)}
          onRetry={() => void markets.refetch()}
        />
      ) : markets.isPending ? (
        <Skeleton className="h-64 rounded-card" />
      ) : data ? (
        <>
          <Card className="flex flex-wrap items-center justify-between gap-3">
            <TokenCell
              symbol={data.token.symbol}
              name={data.token.name}
              address={data.token.address}
              chainId={data.chain.id}
              verified={data.token.verified}
            />
            <span className="trd-figure__value">{formatPrice(data.token.priceUsd)}</span>
          </Card>
          <PriceChart
            chainId={data.chain.id}
            token={data.token.address}
            symbol={data.token.symbol}
          />
          {data.partial ? <p className="trd-callout">{t('webTrading.marketsPartial')}</p> : null}
          {empty ? (
            <p className="trd-note">{t('webTrading.marketsEmpty')}</p>
          ) : (
            <>
              {data.sections.quote.length > 0 ? (
                <PoolTable side="quote" pools={data.sections.quote} symbol={symbol} />
              ) : null}
              {data.sections.base.length > 0 ? (
                <PoolTable side="base" pools={data.sections.base} symbol={symbol} />
              ) : null}
            </>
          )}
        </>
      ) : null}
    </div>
  )
}
