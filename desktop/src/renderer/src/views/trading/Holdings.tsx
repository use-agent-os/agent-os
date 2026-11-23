import {
  ArrowDown,
  ArrowUp,
  ChartCandlestick,
  Coins,
  ArrowLeftRight,
  Eye,
  EyeOff,
  Layers,
  SendHorizontal,
} from 'lucide-react'
import { useState } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { BALANCE_MASK, useBalanceMask } from '~/stores/balance-privacy'
import {
  DUST_USD,
  formatAmount,
  formatAmountCompact,
  formatPct,
  formatPrice,
  formatUsd,
  formatUsdCell,
  holdingMatches,
  pnlTone,
  sameAddress,
  sameToken,
  shortAddress,
  sortHoldings,
  type HoldingSort,
  splitDust,
  splitUnverified,
  walletLabel,
} from './logic'
import { isStockToken } from './markets-logic'
import { UnwrapNote } from './Orders'
import { AssetCell, Empty, ErrorState, Skeleton, Tick } from './parts'
import { isWrappedEth, type Holding, type Wallet } from './types'
import { copyAddress } from './WalletMenu'
import { SearchField } from './WalletSearch'

/**
 * The table grows a search field once it holds enough rows to hunt through.
 * Counted over everything loaded, folded rows included.
 */
export const HOLDING_SEARCH_MIN = 6

/**
 * The ledger's table. Dense mono figures, a hairline per row, the
 * allocation rule under each value, and on hover the two things you do
 * with a holding: look at its chart, send it, or swap it.
 */
export function Holdings({
  holdings,
  loading,
  selected,
  onSelect,
  onSwap,
  onSend,
  onMarkets,
  showChain,
  hiddenCount = 0,
  showHidden = false,
  hiddenLoading = false,
  onToggleHidden,
  onSetHidden,
  error,
  onRetry,
  wallets,
}: {
  holdings: Holding[]
  loading: boolean
  /** The portfolio read failed: shown instead of "nothing held". */
  error?: unknown
  onRetry?: () => void
  /** Every wallet on the desk; with more than one, each row names its own. */
  wallets?: readonly Wallet[]
  selected: Holding | null
  onSelect: (holding: Holding | null) => void
  onSwap: (holding: Holding) => void
  /** Send this holding to an address; the row offers it only when given. */
  onSend?: (holding: Holding) => void
  /** A Stock Token row's Markets action: every pool it trades in (docs/markets.md). */
  onMarkets?: (holding: Holding) => void
  showChain: boolean
  /** Junk tokens the engine keeps out of `holdings` (and the totals). */
  hiddenCount?: number
  /** Whether `holdings` currently includes those, flagged `hidden`. */
  showHidden?: boolean
  /** The junk rows are on their way (they are priced on request, which takes a moment). */
  hiddenLoading?: boolean
  onToggleHidden?: () => void
  /** The user's own hide/show of one token; final as far as the engine is concerned. */
  onSetHidden?: (holding: Holding, hidden: boolean) => void
}) {
  const [sort, setSort] = useState<{ key: HoldingSort; dir: 'asc' | 'desc' }>({
    key: 'value',
    dir: 'desc',
  })
  const [showDust, setShowDust] = useState(false)
  const [showUnverified, setShowUnverified] = useState(false)
  const [query, setQuery] = useState('')
  // One bucket per holding, first match wins: junk (the engine hid it), then
  // unverified (the ⚠, whatever it is worth), then dust. Junk is never dust:
  // it has no value to be under.
  const { kept: listed, unverified } = splitUnverified(holdings.filter((h) => !h.hidden))
  const { kept, dust } = splitDust(listed)
  const junk = holdings.filter((h) => h.hidden)
  // Each holding exactly once, whatever is folded open: a row that appeared
  // twice would give React two children with one key, and React then leaves
  // stale rows behind on the next toggle.
  const { hidden, mask, title } = useBalanceMask()
  // The search narrows what the chips show; it never unfolds a bucket. A
  // folded unverified row is usually a lookalike of the very token searched.
  const searchable = holdings.length >= HOLDING_SEARCH_MIN
  const q = searchable ? query.trim() : ''
  const rows = sortHoldings(
    [...kept, ...(showDust ? dust : []), ...(showUnverified ? unverified : []), ...junk],
    sort.key,
    sort.dir,
  ).filter((h) => holdingMatches(h, q))
  // Matches the folds keep out, so an empty search says where to look.
  const folded = q
    ? [...(showDust ? [] : dust), ...(showUnverified ? [] : unverified)].filter((h) =>
        holdingMatches(h, q),
      ).length
    : 0

  function toggle(key: HoldingSort) {
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === 'desc' ? 'asc' : 'desc' } : { key, dir: 'desc' },
    )
  }

  // One bar, above the table, for everything it is not showing: each kind is
  // a filter chip with its count. Pressed = shown, and the rows appear right
  // under the hand that pressed it. Nothing here is a setting.
  const junkFilter = hiddenCount > 0 && onToggleHidden
  const chips = Boolean(dust.length || unverified.length || junkFilter)
  const filters =
    chips || searchable ? (
      <div className="trd-hiddenbar" data-testid="hidden-bar">
        {searchable ? (
          <SearchField
            value={query}
            onChange={setQuery}
            onSubmit={() => {
              const first = rows[0]
              if (q && first) onSelect(first)
            }}
            placeholder={t('trading.holdings.search')}
            testId="holding-search"
            className="trd-holding-search"
          />
        ) : null}
        {chips ? (
          <div className="trd-hiddenbar__fold">
            <span className="trd-hiddenbar__label">{t('trading.holdings.hidden.label')}</span>
            <div className="trd-hiddenbar__chips">
              {dust.length ? (
                <FilterChip
                  count={dust.length}
                  label={t('trading.holdings.hidden.dust')}
                  help={`${t('trading.holdings.hidden.dust.help')} ${formatUsd(DUST_USD)}`}
                  pressed={showDust}
                  onToggle={() => setShowDust((v) => !v)}
                  testId="dust-toggle"
                />
              ) : null}
              {unverified.length ? (
                <FilterChip
                  count={unverified.length}
                  label={t('trading.holdings.hidden.unverified')}
                  help={t('trading.holdings.hidden.unverified.help')}
                  pressed={showUnverified}
                  onToggle={() => setShowUnverified((v) => !v)}
                  testId="unverified-toggle"
                />
              ) : null}
              {junkFilter ? (
                <FilterChip
                  count={hiddenCount}
                  label={t('trading.holdings.hidden.junk')}
                  help={t('trading.holdings.hidden.junk.help')}
                  pressed={showHidden}
                  busy={hiddenLoading}
                  onToggle={onToggleHidden}
                  testId="junk-toggle"
                />
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    ) : null

  if (error && holdings.length === 0) {
    return <ErrorState error={error} onRetry={onRetry ?? (() => {})} />
  }

  // One wallet on screen, or every row from the same one: no label needed.
  const manyWallets = new Set(holdings.map((h) => h.wallet?.toLowerCase() ?? '')).size > 1
  const walletName = (address: string | null): string | undefined => {
    if (!manyWallets || !address) return undefined
    const w = wallets?.find((x) => sameAddress(x.address, address))
    return w ? walletLabel(w) : shortAddress(address)
  }

  if (!loading && holdings.length === 0) {
    return (
      <>
        {filters}
        <Empty
          icon={<Coins className="size-8" strokeWidth={1.25} aria-hidden />}
          title={t('trading.holdings.empty')}
          body={t('trading.holdings.empty.body')}
        />
      </>
    )
  }

  return (
    <div className="trd-tablewrap">
      {filters}
      <table className="trd-table" aria-label={t('trading.holdings.title')}>
        <thead>
          <tr>
            <Th
              label={t('trading.holdings.col.asset')}
              sortKey="symbol"
              sort={sort}
              onSort={toggle}
              className="trd-col--asset"
            />
            <th className="trd-col--amount">{t('trading.holdings.col.amount')}</th>
            <th className="trd-col--price">{t('trading.holdings.col.price')}</th>
            <Th
              label={t('trading.holdings.col.change')}
              sortKey="change"
              sort={sort}
              onSort={toggle}
              className="trd-col--change"
            />
            <Th
              label={t('trading.holdings.col.value')}
              sortKey="value"
              sort={sort}
              onSort={toggle}
              className="trd-col--value"
            />
            <th className="trd-col--cost">{t('trading.holdings.col.cost')}</th>
            <Th
              label={t('trading.holdings.col.pnl')}
              sortKey="pnl"
              sort={sort}
              onSort={toggle}
              className="trd-col--pnl"
            />
            <th className="trd-col--actions" aria-label={t('trading.holdings.swap')} />
          </tr>
        </thead>
        <tbody>
          {loading && holdings.length === 0
            ? [0, 1, 2, 3].map((i) => (
                <tr key={i}>
                  <td>
                    <Skeleton width={120} />
                  </td>
                  <td className="trd-col--amount">
                    <Skeleton width={64} />
                  </td>
                  <td className="trd-col--price">
                    <Skeleton width={56} />
                  </td>
                  <td className="trd-col--change">
                    <Skeleton width={40} />
                  </td>
                  <td className="trd-col--value">
                    <Skeleton width={72} />
                  </td>
                  <td className="trd-col--cost">
                    <Skeleton width={56} />
                  </td>
                  <td className="trd-col--pnl">
                    <Skeleton width={72} />
                  </td>
                  <td />
                </tr>
              ))
            : rows.map((h) => {
                const key = `${h.chainId}:${h.token.address.toLowerCase()}:${h.wallet ?? ''}`
                const isSelected =
                  selected !== null &&
                  sameToken(selected.token, h.token) &&
                  selected.wallet === h.wallet
                return (
                  <tr
                    key={key}
                    aria-selected={isSelected}
                    data-testid="holding-row"
                    data-hidden={h.hidden ? 'true' : undefined}
                    onClick={() => onSelect(isSelected ? null : h)}
                  >
                    <td className="trd-col--asset">
                      <AssetCell
                        token={h.token}
                        showChain={showChain}
                        tag={h.hidden ? t('trading.holdings.hidden.junk') : undefined}
                        wallet={walletName(h.wallet)}
                        onCopy={
                          h.token.native ? undefined : () => void copyAddress(h.token.address)
                        }
                      />
                    </td>
                    <td className="trd-col--amount" title={title(formatAmount(h.amount, 18))}>
                      {hidden ? (
                        <span className="trd-num">{BALANCE_MASK}</span>
                      ) : (
                        <Tick value={h.amount}>{formatAmountCompact(h.amount)}</Tick>
                      )}
                    </td>
                    <td
                      className={cn('trd-col--price', h.priceUsd === null && 'trd-cell--muted')}
                      title={h.priceUsd === null ? undefined : formatUsd(h.priceUsd)}
                    >
                      <Tick value={h.priceUsd}>
                        {h.priceUsd === null
                          ? t('trading.holdings.noPrice')
                          : formatPrice(h.priceUsd)}
                      </Tick>
                    </td>
                    <td className="trd-col--change" data-tone={pnlTone(h.change24hPct)}>
                      {formatPct(h.change24hPct, { signed: true })}
                    </td>
                    <td className="trd-col--value" title={title(formatUsd(h.valueUsd))}>
                      <span
                        className="trd-value"
                        style={{ ['--alloc' as string]: h.allocationPct }}
                      >
                        {hidden ? (
                          <span className="trd-num">{BALANCE_MASK}</span>
                        ) : (
                          <Tick value={h.valueUsd}>{formatUsdCell(h.valueUsd)}</Tick>
                        )}
                      </span>
                    </td>
                    <td className="trd-col--cost trd-cell--muted" title={formatUsd(h.avgCostUsd)}>
                      {formatUsdCell(h.avgCostUsd)}
                    </td>
                    <td
                      className="trd-col--pnl"
                      data-tone={hidden ? undefined : pnlTone(h.unrealizedUsd)}
                      title={title(formatUsd(h.unrealizedUsd, { signed: true }))}
                    >
                      {mask(formatUsdCell(h.unrealizedUsd, { signed: true }))}
                      {!hidden && h.unrealizedPct !== null ? (
                        <span className="ml-1 text-[10px] opacity-80">
                          {formatPct(h.unrealizedPct, { signed: true })}
                        </span>
                      ) : null}
                    </td>
                    <td className="trd-col--actions">
                      <span className="trd-row__actions">
                        {isWrappedEth(h.token) && h.wallet ? (
                          <UnwrapNote chainId={h.chainId} wallet={h.wallet} compact />
                        ) : null}
                        {onSetHidden && !h.token.native ? (
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`${
                              h.hidden
                                ? t('trading.holdings.showToken')
                                : t('trading.holdings.hideToken')
                            } ${h.token.symbol}`}
                            title={
                              h.hidden
                                ? t('trading.holdings.showToken')
                                : t('trading.holdings.hideToken')
                            }
                            data-testid={h.hidden ? 'show-token' : 'hide-token'}
                            onClick={(e) => {
                              e.stopPropagation()
                              onSetHidden(h, !h.hidden)
                            }}
                          >
                            {h.hidden ? (
                              <Eye
                                className="size-3.5 text-muted-foreground"
                                strokeWidth={1.75}
                                aria-hidden
                              />
                            ) : (
                              <EyeOff
                                className="size-3.5 text-muted-foreground"
                                strokeWidth={1.75}
                                aria-hidden
                              />
                            )}
                          </Button>
                        ) : null}
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`${t('trading.holdings.chart')} ${h.token.symbol}`}
                          title={t('trading.holdings.chart')}
                          onClick={(e) => {
                            e.stopPropagation()
                            onSelect(isSelected ? null : h)
                          }}
                        >
                          <ChartCandlestick
                            className="size-3.5 text-muted-foreground"
                            strokeWidth={1.75}
                            aria-hidden
                          />
                        </Button>
                        {onMarkets && isStockToken(h.token) ? (
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`${t('trading.holdings.markets')} ${h.token.symbol}`}
                            title={t('trading.holdings.markets')}
                            data-testid="holding-markets"
                            onClick={(e) => {
                              e.stopPropagation()
                              onMarkets(h)
                            }}
                          >
                            <Layers
                              className="size-3.5 text-muted-foreground"
                              strokeWidth={1.75}
                              aria-hidden
                            />
                          </Button>
                        ) : null}
                        {onSend && !h.hidden ? (
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`${t('trading.holdings.send')} ${h.token.symbol}`}
                            title={t('trading.holdings.send')}
                            data-testid="holding-send-open"
                            onClick={(e) => {
                              e.stopPropagation()
                              onSend(h)
                            }}
                          >
                            <SendHorizontal
                              className="size-3.5 text-muted-foreground"
                              strokeWidth={1.75}
                              aria-hidden
                            />
                          </Button>
                        ) : null}
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`${t('trading.holdings.swap')} ${h.token.symbol}`}
                          title={t('trading.holdings.swap')}
                          onClick={(e) => {
                            e.stopPropagation()
                            onSwap(h)
                          }}
                        >
                          <ArrowLeftRight
                            className="size-3.5 text-muted-foreground"
                            strokeWidth={1.75}
                            aria-hidden
                          />
                        </Button>
                      </span>
                    </td>
                  </tr>
                )
              })}
        </tbody>
      </table>
      {q && rows.length === 0 && !(loading && holdings.length === 0) ? (
        <p className="trd-table-none" data-testid="holding-search-none">
          {t('trading.holdings.search.none')}
          {folded > 0 ? (
            <span className="text-dim">
              {' · '}
              {t('trading.holdings.search.folded').replace('{count}', String(folded))}
            </span>
          ) : null}
        </p>
      ) : null}
    </div>
  )
}

/**
 * A count + a noun, pressable. Reads as a filter on the table above it:
 * off = these rows are folded away, on = they are in the table, marked.
 */
function FilterChip({
  count,
  label,
  help,
  pressed,
  busy = false,
  onToggle,
  testId,
}: {
  count: number
  label: string
  help: string
  pressed: boolean
  busy?: boolean
  onToggle: () => void
  testId: string
}) {
  return (
    <button
      type="button"
      className="trd-chip app-no-drag"
      aria-pressed={pressed}
      aria-busy={busy || undefined}
      aria-label={`${count} ${label}: ${pressed ? t('trading.holdings.hidden.on') : t('trading.holdings.hidden.off')}`}
      title={help}
      onClick={onToggle}
      data-testid={testId}
    >
      {pressed ? (
        <Eye className="size-3" strokeWidth={2} aria-hidden />
      ) : (
        <EyeOff className="size-3" strokeWidth={2} aria-hidden />
      )}
      <span className="trd-chip__count">{count}</span>
      <span className="trd-chip__label">{label}</span>
    </button>
  )
}

function Th({
  label,
  sortKey,
  sort,
  onSort,
  className,
}: {
  label: string
  sortKey: HoldingSort
  sort: { key: HoldingSort; dir: 'asc' | 'desc' }
  onSort: (key: HoldingSort) => void
  className?: string
}) {
  const active = sort.key === sortKey
  const Arrow = sort.dir === 'desc' ? ArrowDown : ArrowUp
  return (
    <th
      className={className}
      aria-sort={active ? (sort.dir === 'desc' ? 'descending' : 'ascending') : undefined}
    >
      <button type="button" aria-pressed={active} onClick={() => onSort(sortKey)}>
        {label}
        {active ? <Arrow className="size-2.5" strokeWidth={2.5} aria-hidden /> : null}
      </button>
    </th>
  )
}
