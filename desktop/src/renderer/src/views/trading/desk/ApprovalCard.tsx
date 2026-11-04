import { ExternalLink, ShieldAlert, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { copyLpText } from '@/views/chat/transcript/lp'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useTouchIdPrompt } from '~/lib/biometric-gate'
import {
  chainName,
  formatAmount,
  formatUsd,
  initiatorKey,
  isAwaitingApproval,
  shortAddress,
  shortHash,
} from '../logic'
import { StatusPill, Sym } from '../parts'
import { isLpKind, type Order, type Wallet } from '../types'
import {
  approvalFacts,
  askRisk,
  batchFacts,
  lpHook,
  lpPositionLine,
  lpRangeStatus,
  lpRangeText,
  lpStamps,
  orderKind,
  recipientDisplay,
  sumAmounts,
  type Ask,
  type Fact,
  type LpStampKey,
} from './desk-logic'

const ARM_RESET_MS = 4000

const FACT_KEYS = [
  'wallet',
  'chain',
  'pay',
  'receive',
  'minimum',
  'rate',
  'value',
  'impact',
  'gas',
  'provider',
  'order',
  'expires',
  'to',
  'send',
  'token',
  'spender',
  'allowance',
  'recipients',
  'total',
  'batch',
  'unlimited',
  'lpReceive',
  'lpFees',
  'lpDeposit',
  'lpMinimum',
  'lpMaximum',
  'lpApprovals',
  'lpSlippage',
  'fromChain',
  'toChain',
  'bridgeFees',
  'none',
] as const

/** The fact labels, from the catalogue (read inside the component, never at module scope). */
function factLabels(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const key of FACT_KEYS) out[key] = t(`trading.card.fact.${key}`)
  return out
}

function askOf(order: Order, legs: readonly Order[] | undefined): Ask {
  const orders = legs && legs.length ? [...legs] : [order]
  const batch = orders.length > 1 || Boolean(order.batchId && legs && legs.length > 1)
  let totalUsd: number | null = 0
  for (const o of orders) {
    if (o.valueUsd === null) {
      totalUsd = null
      break
    }
    totalUsd += o.valueUsd
  }
  return {
    key: order.batchId || order.orderId,
    kind: orderKind(order),
    lead: order,
    orders,
    batch,
    totalUsd,
    totalAmount: orders.length > 1 ? sumAmounts(orders) : order.amountIn,
  }
}

/**
 * The agent asked; you decide. Facts are bound values from the order, in
 * mono, nothing derived in the renderer: the expiry is printed whole because
 * the engine's sweep owns it. Reject gets focus first (least destructive);
 * a high-risk approve arms and asks for a second click within 4 s.
 *
 * A multisend is one card: `legs` carries every order of the batch, the
 * legs are listed one per line, and the one Approve covers them all — that
 * is what the engine does, so the card may not pretend otherwise.
 *
 * With Touch ID on (Settings › Security), the decision path behind
 * `onApprove` asks for a fingerprint before anything is sent; the card only
 * says "Touch ID…" while the sheet is up, and takes no second click until
 * the decision it started has settled. A cancelled prompt sends nothing and
 * leaves the card live.
 */
export function ApprovalCard({
  order,
  legs,
  wallets,
  deciding,
  onApprove,
  onReject,
  focusOnMount,
  onDismiss,
}: {
  order: Order
  /** Every leg of the batch this order belongs to (the order included). */
  legs?: readonly Order[]
  wallets: readonly Wallet[]
  deciding: boolean
  /** May return the decision's promise: the card ignores Approve until it settles. */
  onApprove: (order: Order, legs?: readonly Order[]) => void | Promise<unknown>
  onReject: (order: Order, reason: string) => void
  focusOnMount: boolean
  /** Settled cards only: close this one now instead of waiting it out. */
  onDismiss?: () => void
}) {
  const ask = askOf(order, legs)
  const risk = askRisk(ask)
  const kind = ask.kind
  const labels = factLabels()
  const facts = ask.batch ? batchFacts(ask, wallets, labels) : approvalFacts(order, wallets, labels)
  const [armed, setArmed] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const rejectRef = useRef<HTMLButtonElement>(null)
  const reasonRef = useRef<HTMLInputElement>(null)
  const focused = useRef(false)
  // One approval at a time: the Touch ID sheet is modal at the OS level, and
  // a second click while it is up (or the decision is in flight) is nothing.
  const inFlight = useRef(false)
  const prompting = useTouchIdPrompt((s) => s.key !== null && s.key === order.orderId)

  useEffect(() => {
    if (focusOnMount && !focused.current) {
      focused.current = true
      rejectRef.current?.focus({ preventScroll: true })
    }
  }, [focusOnMount])

  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), ARM_RESET_MS)
    return () => window.clearTimeout(id)
  }, [armed])

  useEffect(() => {
    if (rejecting) reasonRef.current?.focus({ preventScroll: true })
  }, [rejecting])

  function approve() {
    if (inFlight.current || deciding) return
    if (risk === 'high' && !armed) {
      setArmed(true)
      return
    }
    setArmed(false)
    inFlight.current = true
    const release = () => {
      inFlight.current = false
    }
    try {
      const pending = onApprove(order, legs)
      if (pending instanceof Promise) void pending.then(release, release)
      else release()
    } catch (err) {
      release()
      throw err
    }
  }

  function reject() {
    if (!rejecting) {
      setRejecting(true)
      return
    }
    onReject(order, reason)
  }

  const live = isAwaitingApproval(order)
  // The reason reaches an agent only through the session that asked.
  const reasonPrompt = order.sessionKey
    ? t('trading.card.reasonPlaceholder')
    : t('trading.card.reasonPlaceholder.optional')
  const titleKind = ask.batch ? 'multisend' : kind
  const title = live
    ? titleKind === 'swap'
      ? t('trading.card.title')
      : t(`trading.card.title.${titleKind}`)
    : titleKind === 'swap'
      ? t('trading.card.titleSettled')
      : t(`trading.card.titleSettled.${titleKind}`)
  // A settled batch: the status of the whole is the worst of its legs.
  const settledStatus = ask.batch
    ? (ask.orders.find((o) => o.status === 'failed')?.status ??
      ask.orders.find((o) => o.status !== 'confirmed')?.status ??
      order.status)
    : order.status

  return (
    <article
      className="trd-card"
      data-risk={risk}
      data-kind={kind}
      data-batch={ask.batch || undefined}
      data-testid="approval-card"
      data-order={order.orderId}
      aria-labelledby={`trd-card-${order.orderId}`}
    >
      <header className="trd-card__head">
        <h3 id={`trd-card-${order.orderId}`} className="trd-card__title">
          {title}
        </h3>
        <span className="trd-card__stamps">
          <span className="trd-stamp">{t(`trading.card.by.${initiatorKey(order.initiator)}`)}</span>
          {(kind === 'send' || kind === 'bridge') && live ? (
            <span className="trd-stamp" data-tone="warn" data-testid="stamp-irreversible">
              {t('trading.card.irreversible')}
            </span>
          ) : null}
          {live
            ? lpStamps(order).map((stamp) => (
                <span
                  key={stamp.key}
                  className="trd-stamp"
                  data-tone="warn"
                  data-testid={`stamp-lp-${stamp.key}`}
                  title={stamp.key === 'hook' ? (lpHook(order.plan) ?? undefined) : undefined}
                >
                  {lpStampText(stamp.key, stamp.token)}
                </span>
              ))
            : null}
          {risk === 'high' ? (
            <span className="trd-stamp" data-tone="danger" data-testid="risk-high">
              <ShieldAlert className="size-3" strokeWidth={2} aria-hidden />
              {t('trading.card.high')}
            </span>
          ) : null}
          {!live ? <StatusPill status={settledStatus} /> : null}
          {onDismiss ? (
            <button
              type="button"
              className="trd-card__dismiss app-no-drag"
              onClick={onDismiss}
              aria-label={t('trading.card.dismiss')}
              title={t('trading.card.dismiss')}
              data-testid="card-dismiss"
            >
              <X className="size-3" strokeWidth={2} aria-hidden />
            </button>
          ) : null}
        </span>
      </header>

      <Legs ask={ask} />

      <dl className="trd-card__facts">
        {facts.map((f: Fact) => (
          <div
            key={f.key}
            // An address fact takes its own line, in full: a shortened
            // recipient is exactly where a lookalike hides.
            className={f.wide ? 'trd-card__fact trd-card__fact--wide' : 'trd-card__fact'}
            data-tone={f.tone}
            data-wide={f.wide || undefined}
          >
            <dt>{f.label}</dt>
            <dd className="trd-mono trd-sym" title={f.full}>
              {f.value}
            </dd>
          </div>
        ))}
      </dl>

      {ask.batch ? <Recipients ask={ask} live={live} /> : null}

      {order.note ? (
        // The note is the agent's free text, under its own label and inside
        // a bounded, bidi-isolated block: a multi-kilobyte or right-to-left
        // note may neither push the buttons out of view nor read as one of
        // the facts above it.
        <section className="trd-card__note" data-testid="card-note">
          <span className="trd-card__note-label">
            {t(order.initiator === 'agent' ? 'trading.card.note.agent' : 'trading.card.note')}
          </span>
          <p className="trd-card__note-body" data-testid="card-note-body">
            {order.note}
          </p>
        </section>
      ) : null}
      {order.reason ? (
        // On a settled card this is why it ended as it did; on a live one it
        // is why the engine is asking (over the threshold, a heavy impact,
        // the price moved since the quote) — the one fact that decides the
        // decision, so it may not wait for the outcome to be shown.
        <section className="trd-card__note" data-testid="card-reason">
          <p className="trd-card__note-body">{order.reason}</p>
        </section>
      ) : null}

      {live ? (
        <div className="trd-card__actions">
          {rejecting ? (
            <input
              ref={reasonRef}
              className="mac-input trd-card__reason"
              placeholder={reasonPrompt}
              aria-label={reasonPrompt}
              value={reason}
              maxLength={240}
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  // One decision per ask: a second Enter while it is in flight is nothing.
                  if (!deciding) onReject(order, reason)
                }
                if (e.key === 'Escape') setRejecting(false)
              }}
              data-testid="reject-reason"
            />
          ) : null}
          <span className="trd-card__spacer" />
          <Button
            ref={rejectRef}
            variant="secondary"
            disabled={deciding}
            onClick={reject}
            data-testid="card-reject"
          >
            {rejecting && reason.trim()
              ? t('trading.card.rejectSend')
              : t('trading.approvals.reject')}
          </Button>
          <Button
            variant="primary"
            disabled={deciding}
            onClick={approve}
            data-armed={armed || undefined}
            data-touch-id={prompting || undefined}
            aria-busy={prompting || undefined}
            data-testid="card-approve"
          >
            {prompting
              ? t('trading.touchId.prompting')
              : armed
                ? t('trading.card.approveAgain')
                : t('trading.approvals.approve')}
          </Button>
        </div>
      ) : kind === 'bridge' && order.bridge?.destinationTxHash && order.txHash ? (
        // A settled bridge has two receipts: the deposit on the origin chain
        // and the fill on the destination. Both are links, each labelled.
        <div className="trd-card__actions trd-card__actions--bridge" data-testid="card-bridge-txs">
          <span className="trd-card__spacer" />
          <span className="trd-card__txlabel">{t('trading.card.fact.deposit')}</span>
          <TxLink hash={order.txHash} url={order.explorerUrl} />
          <span className="trd-card__txlabel">{t('trading.card.fact.arrival')}</span>
          <TxLink
            hash={order.bridge.destinationTxHash}
            url={order.bridge.destinationExplorerUrl ?? null}
          />
        </div>
      ) : order.txHash && !ask.batch ? (
        <div className="trd-card__actions">
          <span className="trd-card__spacer" />
          <TxLink hash={order.txHash} url={order.explorerUrl} />
        </div>
      ) : null}
    </article>
  )
}

/**
 * A settled card's tx hash, the same as the ledger row's: with an explorer
 * page it is a real link (a new tab, which the shell hands to the default
 * browser) — hover shows where it goes and it can be copied as a link;
 * without one it copies the hash, so it never does nothing.
 */
function TxLink({ hash, url }: { hash: string; url: string | null }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const id = window.setTimeout(() => setCopied(false), 1_500)
    return () => window.clearTimeout(id)
  }, [copied])
  if (url) {
    return (
      <a
        className="trd-card__link app-no-drag"
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        title={`${t('trading.ledger.openTx')} · ${hash}`}
        data-testid="card-tx"
        data-tx="link"
      >
        {shortHash(hash)}
        <ExternalLink className="size-3" strokeWidth={1.75} aria-hidden />
      </a>
    )
  }
  return (
    <button
      type="button"
      className="trd-card__link app-no-drag"
      title={`${t('trading.ledger.copyTx')} · ${hash}`}
      data-testid="card-tx"
      data-tx="copy"
      onClick={() =>
        void copyLpText(hash).then(
          () => setCopied(true),
          () => {},
        )
      }
    >
      {copied ? t('trading.ledger.copied') : shortHash(hash)}
    </button>
  )
}

function lpStampText(key: LpStampKey, token?: string): string {
  if (key === 'burns') return t('trading.card.lp.burns')
  if (key === 'hook') return t('trading.card.lp.hook')
  if (key === 'noFees') return t('trading.card.lp.noFees')
  return t('trading.card.lp.oneSided').replace('{token}', token ?? '')
}

const LP_STATUS_WORD = {
  'in-range': 'trading.card.lp.inRange',
  'above-range': 'trading.card.lp.aboveRange',
  'below-range': 'trading.card.lp.belowRange',
} as const

/**
 * An LP write's headline: the position it acts on (`#48213 · PEPE / WETH ·
 * Base · 1%`), then the range with where the price sits against it.
 */
function LpLegs({ order }: { order: Order }) {
  const plan = order.plan
  const status = plan ? lpRangeStatus(plan) : null
  const range = plan
    ? lpRangeText(plan, { mcap: t('trading.card.lp.mcap'), per: t('trading.card.lp.per') })
    : ''
  return (
    <div className="trd-card__lp" data-testid="card-legs">
      <p className="trd-card__legs trd-num" data-testid="card-lp-position">
        <b>{lpPositionLine(order, t('trading.card.lp.newPosition'))}</b>
        {orderKind(order) === 'lp_remove' && typeof plan?.pct === 'number' ? (
          <span className="trd-card__lp-pct">{plan.pct}%</span>
        ) : null}
      </p>
      {range ? (
        <p className="trd-card__lp-range trd-mono" data-testid="card-lp-range">
          <span>{range}</span>
          {status ? (
            <span className="trd-card__lp-pill" data-status={status} data-testid="card-lp-status">
              {t(LP_STATUS_WORD[status])}
            </span>
          ) : null}
        </p>
      ) : null}
    </div>
  )
}

/** The headline: what moves, to where. */
function Legs({ ask }: { ask: Ask }) {
  const lead = ask.lead
  if (isLpKind(ask.kind)) return <LpLegs order={lead} />
  if (ask.kind === 'revoke') {
    return (
      <p className="trd-card__legs trd-num" data-testid="card-legs">
        <b>
          {lead.tokenIn.symbol ? (
            <Sym symbol={lead.tokenIn.symbol} />
          ) : (
            shortAddress(lead.tokenIn.address)
          )}
        </b>
        <span aria-hidden>⛨</span>
        <b>{recipientDisplay(lead) || shortAddress(lead.recipient ?? '')}</b>
      </p>
    )
  }
  if (ask.kind === 'bridge') {
    // "0.01 ETH → Robinhood Chain · ≈ 0.0099 ETH · fees $0.12": the same
    // asset on the other chain, what Relay expects to deliver, what it keeps.
    const bridge = lead.bridge ?? null
    const to = bridge?.destinationChain?.name ?? chainName(bridge?.destinationChainId ?? NaN)
    const confirmed = lead.status === 'confirmed'
    const out = confirmed ? (lead.receivedOut ?? lead.expectedOut) : lead.expectedOut
    const fees = bridge?.feesUsd ?? null
    return (
      <p className="trd-card__legs trd-num" data-testid="card-legs">
        <b>
          {formatAmount(lead.amountIn)} <Sym symbol={lead.tokenIn.symbol} />
        </b>
        <span aria-hidden>→</span>
        <b>{to}</b>
        {out ? (
          <span className="trd-card__legs-sub" data-testid="card-bridge-out">
            {confirmed ? '' : '≈ '}
            {formatAmount(out)} <Sym symbol={lead.tokenOut.symbol} />
          </span>
        ) : null}
        {fees !== null ? (
          <span className="trd-card__legs-sub" data-testid="card-bridge-fees">
            {t('trading.card.fact.bridgeFees').toLowerCase()} {formatUsd(fees)}
          </span>
        ) : null}
      </p>
    )
  }
  if (ask.kind === 'send') {
    return (
      <p className="trd-card__legs trd-num" data-testid="card-legs">
        <b>
          {formatAmount(ask.totalAmount)} <Sym symbol={lead.tokenIn.symbol} />
        </b>
        <span aria-hidden>→</span>
        <b>
          {ask.batch
            ? `${ask.orders.length} ${t('trading.send.count')}`
            : lead.recipientLabel || shortAddress(lead.recipient ?? '')}
        </b>
      </p>
    )
  }
  return (
    <p className="trd-card__legs trd-num" data-testid="card-legs">
      <b>
        {formatAmount(lead.amountIn)} <Sym symbol={lead.tokenIn.symbol} />
      </b>
      <span aria-hidden>→</span>
      <b>
        {lead.expectedOut ? `${formatAmount(lead.expectedOut)} ` : ''}
        <Sym symbol={lead.tokenOut.symbol} />
      </b>
    </p>
  )
}

/**
 * Every leg of a multisend, address in full: the one thing a person must
 * read before approving is where the money goes, and a shortened address
 * is exactly where a lookalike hides.
 */
function Recipients({ ask, live }: { ask: Ask; live: boolean }) {
  return (
    <details
      className="trd-card__legs-list"
      open={ask.orders.length <= 8}
      data-testid="card-legs-list"
    >
      <summary>{t('trading.card.recipients')}</summary>
      <ol>
        {ask.orders.map((leg) => (
          <li key={leg.orderId} data-status={leg.status}>
            <span className="trd-mono trd-card__addr">{leg.recipient ?? ''}</span>
            <b className="trd-num">
              {formatAmount(leg.amountIn)} <Sym symbol={leg.tokenIn.symbol} />
            </b>
            {!live ? (
              leg.txHash ? (
                <TxLink hash={leg.txHash} url={leg.explorerUrl} />
              ) : leg.status === 'failed' ? (
                <span className="trd-card__legfail" title={leg.reason ?? ''}>
                  {t('trading.card.leg.failed')}
                </span>
              ) : null
            ) : null}
          </li>
        ))}
      </ol>
    </details>
  )
}
