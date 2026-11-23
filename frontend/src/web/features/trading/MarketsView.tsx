import { Search, Store } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/webTrading'
import {
  chainName,
  errorText,
  formatPct,
  formatPrice,
  pnlTone,
  shortAddress,
} from '@/views/trading/logic'
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
import { EmptyState, ErrorState, Skeleton, useDelayed } from '@/web/components/ui/states'
import { ambiguousCandidates, useMarkets, type SymbolCandidate } from './data'
import { TokenCell, TokenMark } from './parts'
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

/**
 * A symbol several tokens share: the engine names them and the user picks
 * one, which reads the same network by address. Text only, no logos.
 */
function CandidatePicker({
  candidates,
  omitted,
  onPick,
}: {
  candidates: SymbolCandidate[]
  omitted: number
  onPick: (address: string) => void
}) {
  const heading = useId()
  return (
    <Card className="flex flex-col gap-3" data-testid="trd-candidates">
      <p id={heading} className="font-medium">
        {t('webTrading.marketsAmbiguous')}
      </p>
      <ul className="trd-candidates" aria-labelledby={heading}>
        {candidates.map((c) => (
          <li key={c.address.toLowerCase()}>
            <button
              type="button"
              className="trd-row-button trd-candidate"
              title={c.address}
              onClick={() => onPick(c.address)}
            >
              <TokenMark symbol={c.symbol} address={c.address} />
              <span className="trd-token__text">
                <span className="trd-token__symbol">
                  <span className="truncate">{clampSym(c.symbol || c.address.slice(0, 8))}</span>
                </span>
                {c.name ? (
                  <span className="trd-token__sub">
                    <span className="truncate">{clampText(c.name, 40)}</span>
                  </span>
                ) : null}
              </span>
              <span className="trd-num trd-mono trd-note trd-candidate__address" aria-hidden>
                {shortAddress(c.address)}
              </span>
              <span className="sr-only">{c.address}</span>
            </button>
          </li>
        ))}
      </ul>
      {omitted > 0 ? (
        <p className="trd-note">{tPlural('webTrading.marketsAmbiguousMore', omitted)}</p>
      ) : null}
    </Card>
  )
}

/** How long a markets read runs before the page says why it is slow. */
export const MARKETS_SLOW_MS = 4_000

/**
 * The skeleton while pools are read. A rate-limited price source can hold
 * `trading.markets` for 15 s or more; past a few seconds a line says so, so
 * the grey block does not read as a hang. It mounts with each read, so a new
 * search starts the clock again.
 */
function MarketsLoading() {
  const slow = useDelayed(MARKETS_SLOW_MS)
  return (
    <div className="flex flex-col gap-2">
      <Skeleton className="h-64 rounded-card" />
      {slow ? (
        <p className="trd-note" role="status">
          {t('webTrading.marketsSlow')}
        </p>
      ) : null}
    </div>
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

  const choices =
    markets.isError && params ? ambiguousCandidates(markets.error, params.chainId) : null

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
      ) : choices ? (
        <CandidatePicker
          candidates={choices.shown}
          omitted={choices.omitted}
          onPick={(address) => setParams({ target: address, chainId: params.chainId })}
        />
      ) : markets.isError ? (
        <ErrorState
          title={t('webTrading.marketsFailed')}
          message={errorText(markets.error)}
          onRetry={() => void markets.refetch()}
        />
      ) : markets.isPending ? (
        <MarketsLoading />
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
