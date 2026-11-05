import { ChainBadge } from './ChainMark'
import {
  ArrowRight,
  Ban,
  Check,
  CircleDashed,
  ExternalLink,
  Hourglass,
  ListChecks,
  PackageOpen,
  Search,
  SendHorizontal,
  ShieldAlert,
  ShieldCheck,
  TimerOff,
  TriangleAlert,
  X,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useTouchIdPrompt } from '~/lib/biometric-gate'
import { desktopApi } from '~/lib/desktop-api'
import { shortAge } from '~/lib/relative-time'
import { useNow } from '~/lib/use-now'
import { useUnwrap } from '~/stores/trading'
import { HIGH_RISK_USD, lpOrderAmounts, lpOrderLine } from './desk/desk-logic'
import {
  approvalSecondsLeft,
  chainName,
  errorText,
  formatAmount,
  formatClock,
  formatPct,
  formatUsd,
  formatUsdCell,
  impactTone,
  initiatorKey,
  isAwaitingApproval,
  orderTone,
  shortAddress,
} from './logic'
import { Empty, ErrorState, Sym } from './parts'
import { isWrappedEth, providerLabel, type Order, type OrderStatus } from './types'

/** How long an armed Approve waits for its second click before it relaxes. */
const ARM_RESET_MS = 4000

/** One mark per state, so a column of rows is scannable before it is read. */
const GLYPH: Record<OrderStatus, LucideIcon> = {
  quoted: CircleDashed,
  awaiting_approval: ShieldAlert,
  approved: ShieldCheck,
  submitted: SendHorizontal,
  confirmed: Check,
  failed: X,
  rejected: Ban,
  expired: TimerOff,
}

/** "Received WETH" with the one click that turns it back into ETH. */
export function UnwrapNote({
  chainId,
  wallet,
  amount,
  compact,
}: {
  chainId: number
  wallet: string
  amount?: string
  compact?: boolean
}) {
  const unwrap = useUnwrap()
  return (
    <span className="inline-flex items-center gap-2">
      {!compact ? <span>{t('trading.unwrap.note')}</span> : null}
      <Button
        disabled={unwrap.isPending}
        data-testid="unwrap"
        aria-label={compact ? t('trading.unwrap.cta') : undefined}
        title={t('trading.unwrap.cta')}
        size={compact ? 'icon' : 'md'}
        variant={compact ? 'ghost' : 'secondary'}
        onClick={(e) => {
          e.stopPropagation()
          unwrap.mutate(
            { chainId, wallet, ...(amount ? { amount } : {}) },
            {
              onSuccess: () => toast.success(t('trading.unwrap.sent'), { id: 'trd-unwrap' }),
              onError: (err) =>
                toast.error(`${t('trading.unwrap.failed')}: ${errorText(err)}`, {
                  id: 'trd-unwrap',
                }),
            },
          )
        }}
      >
        <PackageOpen
          className={compact ? 'size-3.5 text-muted-foreground' : 'size-3.5'}
          strokeWidth={1.75}
          aria-hidden
        />
        {!compact ? t('trading.unwrap.cta') : null}
      </Button>
    </span>
  )
}

/**
 * Orders, and the approvals view which is the same rows filtered to what
 * the agent is waiting on: each with its timer, Approve and Reject.
 */
export function Orders({
  orders,
  approvalsOnly,
  deciding,
  onDecide,
  showWallet,
  highlight,
  onHighlighted,
  onInspect,
  error,
  onRetry,
}: {
  orders: Order[]
  approvalsOnly: boolean
  deciding: string | null
  /** May return the decision's promise: Approve is ignored until it settles. */
  onDecide: (order: Order, approve: boolean) => void | Promise<unknown>
  showWallet: boolean
  /** An order id to scroll to (from a notification). */
  highlight: string | null
  /** The row has been brought into view: the owner may drop the highlight. */
  onHighlighted?: () => void
  /** Open the decoder on this order's transaction. */
  onInspect?: (order: Order) => void
  /** The list read failed: shown instead of "no orders". */
  error?: unknown
  onRetry?: () => void
}) {
  // Approval timers count down by the second; nothing else on the page does.
  const now = useNow(1000)
  const rows = approvalsOnly ? orders.filter(isAwaitingApproval) : orders
  if (error && orders.length === 0) {
    return <ErrorState error={error} onRetry={onRetry ?? (() => {})} />
  }
  if (rows.length === 0) {
    return approvalsOnly ? (
      <Empty
        icon={<ShieldAlert className="size-8" strokeWidth={1.25} aria-hidden />}
        title={t('trading.approvals.empty')}
        body={t('trading.approvals.empty.body')}
      />
    ) : (
      <Empty
        icon={<ListChecks className="size-8" strokeWidth={1.25} aria-hidden />}
        title={t('trading.orders.empty')}
        body={t('trading.orders.empty.body')}
      />
    )
  }
  return (
    <div aria-label={approvalsOnly ? t('trading.tab.approvals') : t('trading.tab.orders')}>
      {rows.map((o) => (
        <OrderRow
          key={o.orderId}
          order={o}
          now={now}
          deciding={deciding === o.orderId}
          onDecide={onDecide}
          showWallet={showWallet}
          highlighted={highlight === o.orderId}
          onHighlighted={onHighlighted}
          onInspect={onInspect}
        />
      ))}
    </div>
  )
}

/** A label and its figure on the quiet caption line: "min 0.000148". */
function Fact({
  label,
  title,
  tone,
  children,
}: {
  label: string
  title: string
  tone?: 'warn' | 'danger'
  children: ReactNode
}) {
  return (
    <span className="trd-order__fact" title={title}>
      <span>{label}</span>
      <b data-tone={tone}>{children}</b>
    </span>
  )
}

/** The headline of a row: a swap's two legs, a send's destination, a revoke's spender. */
function Legs({ order }: { order: Order }) {
  const kind = order.kind ?? 'swap'
  if (kind === 'lp_collect' || kind === 'lp_remove' || kind === 'lp_add') {
    // Never `amountIn`: on an LP order it is the liquidity (raw L), not tokens.
    const moved = lpOrderAmounts(order)
    return (
      <>
        <span className="trd-order__leg">
          <i>{t(`trading.orders.kind.${kind}`)}</i> {lpOrderLine(order, { usd: moved === null })}
        </span>
        {moved ? (
          <span
            className="trd-order__leg"
            data-testid="order-lp-amounts"
            data-estimate={order.status !== 'confirmed' ? 'true' : undefined}
            title={moved.full}
          >
            <i>{t(`trading.orders.lp.${moved.side}`)}</i> {moved.text}
          </span>
        ) : null}
      </>
    )
  }
  if (kind === 'send') {
    return (
      <>
        <span className="trd-order__leg">
          {formatAmount(order.amountIn)}{' '}
          <Sym symbol={order.tokenIn.symbol} className="trd-order__sym" />
        </span>
        <ArrowRight className="trd-order__arrow size-3" strokeWidth={2} aria-hidden />
        <span className="trd-order__leg trd-mono" title={order.recipient ?? ''}>
          {order.recipientLabel ?? shortAddress(order.recipient ?? '')}
        </span>
      </>
    )
  }
  if (kind === 'bridge') {
    // The same asset lands on another chain: "0.01 ETH → Robinhood Chain
    // (ETH, est. 0.0099)". Until the destination fills, the amount is
    // Relay's estimate; once confirmed it is what arrived.
    const bridge = order.bridge ?? null
    const confirmed = order.status === 'confirmed'
    const out = confirmed ? (order.receivedOut ?? order.expectedOut) : order.expectedOut
    const to = bridge?.destinationChain?.name ?? chainName(bridge?.destinationChainId ?? NaN)
    const fill = order.status === 'submitted' && bridge?.fillStatus ? bridge.fillStatus : null
    return (
      <>
        <span className="trd-order__leg">
          {formatAmount(order.amountIn)}{' '}
          <Sym symbol={order.tokenIn.symbol} className="trd-order__sym" />
        </span>
        <ArrowRight className="trd-order__arrow size-3" strokeWidth={2} aria-hidden />
        <span
          className="trd-order__leg"
          data-testid="order-bridge-to"
          data-estimate={!confirmed && out ? 'true' : undefined}
        >
          {to}
          {out ? (
            <small className="trd-order__est">
              {' '}
              ({!confirmed ? `${t('trading.orders.est')} ` : ''}
              {formatAmount(out)} <Sym symbol={order.tokenOut.symbol} />)
            </small>
          ) : null}
        </span>
        {fill ? (
          <span
            className="trd-status trd-order__fill"
            data-tone={
              fill === 'success'
                ? 'ok'
                : fill === 'pending'
                  ? 'live'
                  : fill === 'waiting'
                    ? 'dim'
                    : 'danger'
            }
            data-fill={fill}
            data-testid="order-bridge-fill"
            title={bridge?.requestId ?? ''}
          >
            {t(`trading.orders.bridge.fill.${fill}`)}
          </span>
        ) : null}
      </>
    )
  }
  if (kind === 'revoke') {
    return (
      <>
        <span className="trd-order__leg">
          <i>{t('trading.orders.kind.revoke')}</i> <Sym symbol={order.tokenIn.symbol} />
        </span>
        <ArrowRight className="trd-order__arrow size-3" strokeWidth={2} aria-hidden />
        <span className="trd-order__leg trd-mono" title={order.recipient ?? ''}>
          {order.recipientLabel ?? shortAddress(order.recipient ?? '')}
        </span>
      </>
    )
  }
  // A confirmed swap shows what the receipt delivered; anything earlier is
  // still the quote's guess, and says so. Only an older engine's receipt
  // (no `receivedOut` at all) falls back to the estimate once confirmed.
  const confirmed = order.status === 'confirmed'
  const out = confirmed ? (order.receivedOut ?? order.expectedOut) : order.expectedOut
  return (
    <>
      <span className="trd-order__leg">
        {formatAmount(order.amountIn)}{' '}
        <Sym symbol={order.tokenIn.symbol} className="trd-order__sym" />
      </span>
      <ArrowRight className="trd-order__arrow size-3" strokeWidth={2} aria-hidden />
      <span className="trd-order__leg" data-estimate={!confirmed && out ? 'true' : undefined}>
        {out && !confirmed ? (
          <small className="trd-order__est">{t('trading.orders.est')} </small>
        ) : null}
        {out ? `${formatAmount(out)} ` : ''}
        <Sym symbol={order.tokenOut.symbol} className="trd-order__sym" />
      </span>
    </>
  )
}

function OrderRow({
  order,
  now,
  deciding,
  onDecide,
  showWallet,
  highlighted,
  onHighlighted,
  onInspect,
}: {
  order: Order
  now: number
  deciding: boolean
  /** May return the decision's promise: Approve is ignored until it settles. */
  onDecide: (order: Order, approve: boolean) => void | Promise<unknown>
  showWallet: boolean
  highlighted: boolean
  onHighlighted?: () => void
  onInspect?: (order: Order) => void
}) {
  // Scroll once when the highlight lands, not on every tick of the timer.
  const rowRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!highlighted) return
    rowRef.current?.scrollIntoView({ block: 'center' })
    onHighlighted?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlighted])
  const waiting = isAwaitingApproval(order)
  const left = waiting ? approvalSecondsLeft(order, now) : null
  // Above the chat card's high-risk line, Approve here arms first too: the
  // same order must not be one click in one place and two in another.
  const highRisk = order.valueUsd !== null && order.valueUsd >= HIGH_RISK_USD
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), ARM_RESET_MS)
    return () => window.clearTimeout(id)
  }, [armed])
  // The Touch ID sheet (Settings › Security) is modal at the OS level: a
  // second click while it is up, or while the decision is in flight, is nothing.
  const inFlight = useRef(false)
  const prompting = useTouchIdPrompt((s) => s.key !== null && s.key === order.orderId)
  function approve() {
    if (inFlight.current || deciding) return
    if (highRisk && !armed) {
      setArmed(true)
      return
    }
    setArmed(false)
    inFlight.current = true
    const release = () => {
      inFlight.current = false
    }
    try {
      const pending = onDecide(order, true)
      if (pending instanceof Promise) void pending.then(release, release)
      else release()
    } catch (err) {
      release()
      throw err
    }
  }
  const by = initiatorKey(order.initiator)
  const tone = orderTone(order.status)
  const Glyph = GLYPH[order.status]
  const impact = order.priceImpactPct === null ? null : impactTone(order.priceImpactPct)
  const failed =
    order.status === 'failed' || order.status === 'rejected' || order.status === 'expired'
  return (
    <div
      className="trd-order"
      data-status={order.status}
      data-tone={tone}
      data-kind={order.kind ?? 'swap'}
      data-testid="order-row"
      ref={rowRef}
    >
      <span className="trd-order__mark" data-tone={tone} aria-hidden>
        <Glyph className="size-3.5" strokeWidth={2} />
      </span>

      <div className="trd-order__head">
        <Legs order={order} />
      </div>

      <div className="trd-order__value" title={formatUsd(order.valueUsd)}>
        {formatUsdCell(order.valueUsd)}
      </div>

      <div className="trd-order__who">
        <span className="trd-by" data-by={by}>
          {t(`trading.history.by.${by}`)}
        </span>
        <span className="trd-order__time">{shortAge(order.updatedAt, now)}</span>
      </div>

      <div className="trd-order__link">
        {order.txHash && onInspect ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('trading.decode.open')}
            title={t('trading.decode.open')}
            onClick={() => onInspect(order)}
            data-testid="order-inspect"
          >
            <Search className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          </Button>
        ) : null}
        {order.explorerUrl ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('trading.history.explorer')}
            title={t('trading.history.explorer')}
            onClick={() => void desktopApi().app.openExternal(order.explorerUrl as string)}
          >
            <ExternalLink
              className="size-3.5 text-muted-foreground"
              strokeWidth={1.75}
              aria-hidden
            />
          </Button>
        ) : null}
      </div>

      <div className="trd-order__meta">
        <span className="trd-order__state" data-tone={tone}>
          {t(`trading.orders.status.${order.status}`)}
        </span>
        <span className="trd-order__fact">
          <ChainBadge chainId={order.chainId} />
          {order.provider && (order.kind ?? 'swap') === 'swap' ? (
            <span>{`${t('trading.provider.via')} ${providerLabel(order.provider)}`}</span>
          ) : null}
          {order.batchId ? (
            <span className="trd-mono" title={order.batchId}>
              {t('trading.orders.batch')} {order.batchId.slice(4, 10)}
            </span>
          ) : null}
          {order.mandateId ? (
            // A buy a DCA mandate fired: the chip names the mandate it belongs to.
            <span
              className="trd-order__dca trd-mono"
              title={order.mandateId}
              data-testid="order-dca"
            >
              {t('trading.orders.dca')}
            </span>
          ) : null}
          {order.triggerId ? (
            // A swap a price trigger fired: the chip names the trigger it belongs to.
            <span
              className="trd-order__dca trd-order__trigger trd-mono"
              title={order.triggerId}
              data-trigger={order.triggerId}
              data-testid="order-trigger"
            >
              {t('trading.orders.trigger')}
            </span>
          ) : null}
        </span>
        {showWallet ? (
          <span className="trd-order__fact">
            <b className="trd-mono">{shortAddress(order.wallet)}</b>
          </span>
        ) : null}
        {order.minOut ? (
          <Fact label={t('trading.orders.fact.min')} title={t('trading.orders.minimum')}>
            {formatAmount(order.minOut)}
          </Fact>
        ) : null}
        {order.priceImpactPct !== null ? (
          <Fact
            label={t('trading.orders.fact.impact')}
            title={t('trading.swap.impact')}
            tone={impact === 'ok' ? undefined : (impact ?? undefined)}
          >
            {formatPct(order.priceImpactPct)}
          </Fact>
        ) : null}
        {order.gasUsd !== null ? (
          <Fact
            label={t(
              order.status === 'confirmed'
                ? 'trading.orders.fact.fee'
                : 'trading.orders.fact.feeEst',
            )}
            title={t('trading.swap.gas')}
          >
            {formatUsd(order.gasUsd)}
          </Fact>
        ) : null}
      </div>

      {waiting ? (
        <div className="trd-order__act">
          {left !== null ? (
            <span className="trd-order__timer" data-urgent={left <= 60 ? 'true' : undefined}>
              <Hourglass className="size-3" strokeWidth={2} aria-hidden />
              {t('trading.approvals.expiresIn')} {formatClock(left)}
            </span>
          ) : null}
          <div className="trd-order__actions">
            <Button
              disabled={deciding}
              onClick={() => onDecide(order, false)}
              data-testid="order-reject"
            >
              {t('trading.approvals.reject')}
            </Button>
            <Button
              variant="primary"
              disabled={deciding}
              onClick={approve}
              data-armed={armed || undefined}
              data-touch-id={prompting || undefined}
              aria-busy={prompting || undefined}
              data-testid="order-approve"
            >
              {prompting
                ? t('trading.touchId.prompting')
                : armed
                  ? t('trading.card.approveAgain')
                  : t('trading.approvals.approve')}
            </Button>
          </div>
        </div>
      ) : null}

      {order.note ? (
        <div className="trd-order__note">
          {t('trading.orders.note')}: {order.note}
        </div>
      ) : null}

      {order.deliveredToken &&
      order.status === 'confirmed' &&
      isWrappedEth(order.deliveredToken) &&
      !isWrappedEth(order.tokenOut) ? (
        <div className="trd-order__note" data-testid="order-delivered">
          <UnwrapNote chainId={order.chainId} wallet={order.wallet} />
        </div>
      ) : null}

      {order.reason && failed ? (
        <div className="trd-order__reason" title={order.reason}>
          <TriangleAlert className="size-3 shrink-0" strokeWidth={2} aria-hidden />
          <span>
            {t('trading.orders.reason')}: {order.reason}
          </span>
        </div>
      ) : null}
    </div>
  )
}
