import { Droplets, RefreshCw, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { shortAge } from '~/lib/relative-time'
import { cn } from '~/lib/utils'
import { ChainMark } from './ChainMark'
import {
  filterByProtocol,
  formatRate,
  formatShare,
  hasClaimable,
  type LpAction,
  lpActions,
  lpStatusTone,
  positionTag,
  rangeMarker,
} from './liquidity-logic'
import {
  formatAmountCompact,
  formatUsd,
  formatUsdCell,
  sameAddress,
  shortAddress,
  walletLabel,
} from './logic'
import { BALANCE_MASK, useBalanceMask } from '~/stores/balance-privacy'
import { Empty, ErrorState, Skeleton, Spinner, Tick, TokenLogo } from './parts'
import type { LpBook, LpPosition, LpToken, Token, Wallet } from './types'
import { copyAddress } from './WalletMenu'

/**
 * Where the desk's wallets have liquidity: one row per position across
 * Uniswap, Aero and Aerodrome. The engine keeps the book (it reads the chain
 * only while this tab is open, and serves its cache in between); this lays
 * it out. Out-of-range positions come first, because those are the ones
 * that need a decision.
 */
export function Liquidity({
  book,
  loading,
  error,
  onRetry,
  onRefresh,
  refreshing,
  wallets,
  showChain,
  logos,
  now,
  onAction,
  acting,
  layout = 'table',
}: {
  book: LpBook | undefined
  loading: boolean
  /** The read failed: shown instead of "no positions". */
  error?: unknown
  onRetry: () => void
  onRefresh: () => void
  /** The operator's own refresh is on its way (the book's flag covers the engine's). */
  refreshing: boolean
  /** Every wallet on the desk; with more than one, each row names its own. */
  wallets?: readonly Wallet[]
  showChain: boolean
  /** Token art by lower-cased address, borrowed from the holdings. */
  logos?: ReadonlyMap<string, string>
  now: number
  /** Collect a position's fees or take it out; without it the table is read-only. */
  onAction?: (position: LpPosition, action: LpAction) => void
  /** The position (by id) whose action is on its way to the engine. */
  acting?: string | null
  /**
   * `table` is the full desk's six columns. `cards` is for a rail too narrow
   * for them (the BOOK beside the chat): one stacked card per position.
   */
  layout?: 'table' | 'cards'
}) {
  const [protocol, setProtocol] = useState<string | null>(null)
  const positions = book?.positions ?? []
  // A filter whose protocol went away (the position was closed) falls back to All.
  const { mask } = useBalanceMask()
  const active = protocol && book?.protocols.some((p) => p.key === protocol) ? protocol : null
  const rows = filterByProtocol(positions, active)
  const busy = refreshing || Boolean(book?.refreshing)

  if (error && !book) return <ErrorState error={error} onRetry={onRetry} />

  const manyWallets = new Set(positions.map((p) => p.owner.address.toLowerCase())).size > 1
  const walletName = (position: LpPosition): string | undefined => {
    if (!manyWallets) return undefined
    const found = wallets?.find((w) => sameAddress(w.address, position.owner.address))
    return found
      ? walletLabel(found)
      : (position.owner.label ?? shortAddress(position.owner.address))
  }

  const summary = book ? (
    <div className="trd-lpbar" data-testid="lp-summary">
      <Stat
        label={t('trading.liquidity.total')}
        value={mask(formatUsd(book.totals.valueUsd))}
        strong
      />
      <Stat label={t('trading.liquidity.fees')} value={mask(formatUsdCell(book.totals.feesUsd))} />
      {book.totals.rewardsUsd ? (
        <Stat
          label={t('trading.liquidity.rewards')}
          value={mask(formatUsdCell(book.totals.rewardsUsd))}
        />
      ) : null}
      <Stat
        label={t('trading.liquidity.count')}
        value={String(book.totals.count)}
        note={
          book.totals.outOfRange > 0
            ? `${book.totals.outOfRange} ${t('trading.liquidity.outOfRange')}`
            : undefined
        }
      />
      <span className="trd-lpbar__spacer" />
      {book.partialScan ? (
        <span className="trd-lpbar__warn" title={book.warnings.join('\n')} data-testid="lp-partial">
          <TriangleAlert className="size-3" strokeWidth={2} aria-hidden />
          {t('trading.liquidity.partial')}
        </span>
      ) : null}
      <span className="trd-lpbar__age" data-live={busy ? 'true' : undefined}>
        {busy ? (
          <>
            <Spinner className="size-3" />
            {t('trading.liquidity.refreshing')}
          </>
        ) : (
          <>
            {t('trading.liquidity.updated')}{' '}
            {book.readAt ? shortAge(book.readAt * 1000, now) : t('trading.never')}
          </>
        )}
      </span>
      <Button
        variant="ghost"
        size="icon"
        aria-label={t('trading.liquidity.refresh')}
        title={t('trading.liquidity.refresh')}
        disabled={busy}
        onClick={onRefresh}
        data-testid="lp-refresh"
      >
        <RefreshCw className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
      </Button>
    </div>
  ) : null

  const filters =
    book && book.protocols.length > 1 ? (
      <div
        className="trd-lpfilter"
        role="radiogroup"
        aria-label={t('trading.liquidity.filter.label')}
      >
        <ProtocolChip
          label={t('trading.liquidity.filter.all')}
          count={book.totals.count}
          checked={active === null}
          onPick={() => setProtocol(null)}
        />
        {book.protocols.map((p) => (
          <ProtocolChip
            key={p.key}
            label={p.name}
            count={p.count}
            checked={active === p.key}
            onPick={() => setProtocol(p.key)}
          />
        ))}
      </div>
    ) : null

  // Nothing read yet: say that it is being read, not that there is nothing
  // (and no totals either: a "$0.00 in liquidity" here would be a guess).
  if (book && !book.ready && positions.length === 0) {
    return (
      <>
        <Empty
          icon={<Spinner className="size-7" />}
          title={t('trading.liquidity.reading')}
          body={t('trading.liquidity.reading.body')}
        />
      </>
    )
  }
  if (!loading && positions.length === 0) {
    return (
      <>
        {summary}
        <Empty
          icon={<Droplets className="size-8" strokeWidth={1.25} aria-hidden />}
          title={t('trading.liquidity.empty')}
          body={t('trading.liquidity.empty.body')}
        />
      </>
    )
  }

  if (layout === 'cards') {
    return (
      <div className="trd-lpcards" data-testid="lp-cards">
        {summary}
        {filters}
        <ul className="trd-lpcards__list" aria-label={t('trading.liquidity.title')}>
          {loading && positions.length === 0
            ? [0, 1, 2].map((i) => (
                <li key={i} className="trd-lpcard" aria-hidden>
                  <div className="trd-lpcard__head">
                    <Skeleton width={150} />
                    <Skeleton width={72} />
                  </div>
                  <Skeleton width={200} />
                  <Skeleton width={120} />
                </li>
              ))
            : rows.map((position) => (
                <Card
                  key={position.id}
                  position={position}
                  showChain={showChain}
                  wallet={walletName(position)}
                  logos={logos}
                  onAction={onAction}
                  busy={acting === position.id}
                  locked={Boolean(acting)}
                />
              ))}
        </ul>
      </div>
    )
  }

  return (
    <div className="trd-tablewrap">
      {summary}
      {filters}
      <table className="trd-table trd-lptable" aria-label={t('trading.liquidity.title')}>
        <thead>
          <tr>
            <th className="trd-col--asset">{t('trading.liquidity.col.pool')}</th>
            <th className="trd-col--range">{t('trading.liquidity.col.range')}</th>
            <th className="trd-col--amounts">{t('trading.liquidity.col.amounts')}</th>
            <th className="trd-col--earned">{t('trading.liquidity.col.earned')}</th>
            <th className="trd-col--value">{t('trading.liquidity.col.value')}</th>
            {onAction ? (
              <th className="trd-col--lpact">
                <span className="sr-only">{t('trading.liquidity.col.actions')}</span>
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {loading && positions.length === 0
            ? [0, 1, 2].map((i) => (
                <tr key={i}>
                  <td>
                    <Skeleton width={150} />
                  </td>
                  <td className="trd-col--range">
                    <Skeleton width={120} />
                  </td>
                  <td className="trd-col--amounts">
                    <Skeleton width={90} />
                  </td>
                  <td className="trd-col--earned">
                    <Skeleton width={60} />
                  </td>
                  <td className="trd-col--value">
                    <Skeleton width={72} />
                  </td>
                  {onAction ? <td className="trd-col--lpact" /> : null}
                </tr>
              ))
            : rows.map((position) => (
                <Row
                  key={position.id}
                  position={position}
                  showChain={showChain}
                  wallet={walletName(position)}
                  logos={logos}
                  onAction={onAction}
                  busy={acting === position.id}
                  locked={Boolean(acting)}
                />
              ))}
        </tbody>
      </table>
    </div>
  )
}

function Stat({
  label,
  value,
  note,
  strong,
}: {
  label: string
  value: string
  note?: string
  strong?: boolean
}) {
  return (
    <span className="trd-lpstat" data-strong={strong ? 'true' : undefined}>
      <span className="trd-lpstat__label">{label}</span>
      <span className="trd-lpstat__value">{value}</span>
      {note ? <span className="trd-lpstat__note">{note}</span> : null}
    </span>
  )
}

function ProtocolChip({
  label,
  count,
  checked,
  onPick,
}: {
  label: string
  count: number
  checked: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      className="trd-chip app-no-drag"
      onClick={onPick}
    >
      {label}
      <span className="trd-chip__count">{count}</span>
    </button>
  )
}

function asToken(token: LpToken, chainId: number, logos?: ReadonlyMap<string, string>): Token {
  return {
    chainId,
    address: token.address,
    symbol: token.symbol,
    name: token.symbol,
    decimals: token.decimals,
    logoUrl: logos?.get(token.address.toLowerCase()) ?? null,
    native: false,
    verified: true,
  }
}

interface RowProps {
  position: LpPosition
  showChain: boolean
  wallet?: string
  logos?: ReadonlyMap<string, string>
  onAction?: (position: LpPosition, action: LpAction) => void
  /** This row's action is on its way. */
  busy: boolean
  /** Some row's action is on its way: one order at a time. */
  locked: boolean
}

/** The pair, where it lives and whose it is: the same ident in a row and on a card. */
function PoolIdent({
  position,
  showChain,
  wallet,
  logos,
}: Pick<RowProps, 'position' | 'showChain' | 'wallet' | 'logos'>) {
  const meta = [position.protocol.name, position.pool.feePct, positionTag(position)]
    .filter(Boolean)
    .join(' · ')
  return (
    <span className="trd-lppool">
      <span className="trd-lppool__logos" aria-hidden>
        <TokenLogo token={asToken(position.token, position.chain.id, logos)} size={20} />
        <TokenLogo token={asToken(position.quote, position.chain.id, logos)} size={20} />
      </span>
      <span className="trd-asset__text">
        <span className="trd-asset__symbol">
          <button
            type="button"
            className="trd-lppool__pair app-no-drag"
            title={`${t('trading.liquidity.copyPool')}: ${position.pool.poolId}`}
            onClick={() => void copyAddress(position.pool.poolId)}
          >
            {position.token.symbol} / {position.quote.symbol}
          </button>
          {showChain ? (
            <span className="trd-asset__chainmark" title={position.chain.name}>
              <ChainMark chainId={position.chain.id} />
            </span>
          ) : null}
          {position.staked ? (
            <span className="trd-lptag" title={t('trading.liquidity.staked.help')}>
              {t('trading.liquidity.staked')}
            </span>
          ) : null}
        </span>
        <span className="trd-asset__name" title={meta}>
          {wallet ? (
            <span className="trd-asset__wallet trd-mono" data-testid="asset-wallet">
              {wallet}
            </span>
          ) : null}
          {meta}
        </span>
      </span>
    </span>
  )
}

function Deposited({ position }: { position: LpPosition }) {
  const { mask } = useBalanceMask()
  return (
    <span className="trd-lpstack">
      <span>
        {mask(formatAmountCompact(position.principal.base.human))} {position.token.symbol}
      </span>
      <span>
        {mask(formatAmountCompact(position.principal.quote.human))} {position.quote.symbol}
      </span>
    </span>
  )
}

function Earned({ position }: { position: LpPosition }) {
  const { mask } = useBalanceMask()
  return (
    <span className="trd-lpstack">
      <span>{position.fees ? mask(formatUsdCell(position.fees.usd)) : '—'}</span>
      {position.rewards ? (
        <span className="trd-lpstack__reward">
          {mask(formatAmountCompact(position.rewards.amount.human))} {position.rewards.token.symbol}
        </span>
      ) : null}
    </span>
  )
}

function earnedTitle(position: LpPosition): string | undefined {
  return position.fees === null && !position.rewards ? t('trading.liquidity.noFees') : undefined
}

function Value({ position }: { position: LpPosition }) {
  const { hidden } = useBalanceMask()
  if (hidden && position.valueUsd !== null) return <span className="trd-num">{BALANCE_MASK}</span>
  return (
    <Tick value={position.valueUsd}>
      {position.valueUsd === null
        ? t('trading.holdings.noPrice')
        : formatUsdCell(position.valueUsd)}
    </Tick>
  )
}

function Actions({ position, onAction, busy, locked }: RowProps) {
  const actions = onAction ? lpActions(position) : []
  if (!onAction || actions.length === 0) return null
  return (
    <span className="trd-lpact">
      {busy ? <Spinner className="size-3" /> : null}
      {actions.map((action) => (
        <Button
          key={action}
          variant="secondary"
          className="trd-lpact__btn"
          disabled={locked}
          title={t(`trading.liquidity.action.${action}.help`)}
          onClick={() => onAction(position, action)}
          data-testid={`lp-${action}`}
        >
          {t(`trading.liquidity.action.${action}`)}
        </Button>
      ))}
    </span>
  )
}

function Row(props: RowProps) {
  const { position, onAction } = props
  const { title } = useBalanceMask()
  return (
    <tr data-testid="lp-row" data-status={position.status}>
      <td className="trd-col--asset">
        <PoolIdent {...props} />
      </td>
      <td className="trd-col--range">
        <RangeCell position={position} share={formatShare(position.share)} />
      </td>
      <td className="trd-col--amounts" title={title(formatUsd(position.principal.usd))}>
        <Deposited position={position} />
      </td>
      <td
        className={cn('trd-col--earned', !hasClaimable(position) && 'trd-cell--muted')}
        title={earnedTitle(position)}
      >
        <Earned position={position} />
      </td>
      <td className="trd-col--value" title={title(formatUsd(position.valueUsd))}>
        <Value position={position} />
      </td>
      {onAction ? (
        <td className="trd-col--lpact">
          <Actions {...props} />
        </td>
      ) : null}
    </tr>
  )
}

/**
 * The same position for a narrow rail: who and how much on the first line,
 * the range across the full width, then the figures as labelled lines.
 */
function Card(props: RowProps) {
  const { position, onAction } = props
  const { title } = useBalanceMask()
  const hasActions = Boolean(onAction) && lpActions(position).length > 0
  return (
    <li className="trd-lpcard" data-testid="lp-row" data-status={position.status}>
      <div className="trd-lpcard__head">
        <PoolIdent {...props} />
        <span className="trd-lpcard__value" title={title(formatUsd(position.valueUsd))}>
          <Value position={position} />
        </span>
      </div>
      <RangeCell position={position} share={formatShare(position.share)} />
      <dl className="trd-lpcard__figs">
        <div title={title(formatUsd(position.principal.usd))}>
          <dt>{t('trading.liquidity.col.amounts')}</dt>
          <dd>
            <Deposited position={position} />
          </dd>
        </div>
        <div
          className={cn(!hasClaimable(position) && 'trd-cell--muted')}
          title={earnedTitle(position)}
        >
          <dt>{t('trading.liquidity.col.earned')}</dt>
          <dd>
            <Earned position={position} />
          </dd>
        </div>
      </dl>
      {hasActions ? (
        <div className="trd-lpcard__acts">
          <Actions {...props} />
        </div>
      ) : null}
    </li>
  )
}

/**
 * The range as a track with the price on it: the eye reads "comfortably
 * inside" or "about to leave" before it reads a number. A pair has no range,
 * so it shows its share of the pool instead.
 */
function RangeCell({ position, share }: { position: LpPosition; share: string | null }) {
  const tone = lpStatusTone(position.status)
  const marker = rangeMarker(position)
  const status = (
    <span className="trd-status" data-tone={tone === 'dim' ? undefined : tone}>
      {t(`trading.liquidity.status.${position.status}`)}
    </span>
  )
  if (!position.range) {
    return (
      <span className="trd-lprange">
        {status}
        {share ? (
          <span className="trd-lprange__bounds">
            {share} {t('trading.liquidity.share')}
          </span>
        ) : null}
      </span>
    )
  }
  const { priceLower, priceUpper } = position.range
  return (
    <span className="trd-lprange">
      <span className="trd-lprange__head">
        {status}
        {position.distancePct !== null ? (
          <span className="trd-lprange__away">{Math.abs(position.distancePct).toFixed(1)}%</span>
        ) : null}
      </span>
      <span
        className="trd-lprange__track"
        data-tone={tone}
        aria-hidden
        style={marker === null ? undefined : { ['--at' as string]: marker }}
        data-marker={marker === null ? undefined : 'true'}
      />
      <span
        className="trd-lprange__bounds"
        title={`${position.quote.symbol} / ${position.token.symbol}`}
      >
        {formatRate(priceLower)} – {formatRate(priceUpper)} {position.quote.symbol}
      </span>
    </span>
  )
}
