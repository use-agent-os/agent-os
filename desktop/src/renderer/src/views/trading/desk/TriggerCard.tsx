import { Bell, TrendingDown, TrendingUp } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useTouchIdPrompt } from '~/lib/biometric-gate'
import { formatAmount, initiatorKey, sameAddress, shortAddress, walletLabel } from '../logic'
import { Sym } from '../parts'
import type { Trigger, Wallet } from '../types'
import {
  actionText,
  conditionText,
  heroText,
  nowText,
  priceText,
  sizeText,
  triggerNotes,
  usdText,
} from './trigger-logic'
import { useCmdEnter } from '../cmd-enter'

/** "Oct 5 08:00": short enough to sit whole beside its label. */
function shortDate(iso: string | null): string {
  if (!iso) return ''
  const ts = Date.parse(iso)
  if (!Number.isFinite(ts)) return ''
  const parts = new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(ts)
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? ''
  return `${part('month')} ${part('day')} ${part('hour')}:${part('minute')}`
}

function walletName(tr: Trigger, wallets: readonly Wallet[]): string {
  const known = wallets.find((w) => sameAddress(w.address, tr.wallet.address))
  if (known) return walletLabel(known)
  return tr.wallet.label || shortAddress(tr.wallet.address)
}

function walletText(tr: Trigger, wallets: readonly Wallet[]): string {
  const known = wallets.find((w) => sameAddress(w.address, tr.wallet.address))
  if (known) return `${walletLabel(known)} · ${shortAddress(known.address)}`
  return tr.wallet.label
    ? `${tr.wallet.label} · ${shortAddress(tr.wallet.address)}`
    : shortAddress(tr.wallet.address)
}

const GLYPH = { sell: TrendingDown, buy: TrendingUp, alert: Bell } as const

/**
 * A price trigger the agent proposed, waiting for the operator
 * (docs/triggers.md). One decision arms it, and it may trade later with
 * nobody watching, so the card says the whole of it — what, when, how much,
 * from which wallet, until when — before the one button that arms it. The
 * engine enforces all of it; the card only reads.
 */
export function TriggerCard({
  trigger: tr,
  wallets,
  deciding,
  onApprove,
  onReject,
  focusOnMount = false,
  warnings = [],
}: {
  trigger: Trigger
  wallets: readonly Wallet[]
  /** The engine's own warnings for this trigger, when the caller has them. */
  warnings?: readonly string[]
  deciding: boolean
  onApprove: (tr: Trigger) => void
  onReject: (tr: Trigger) => void
  focusOnMount?: boolean
}) {
  const rejectRef = useRef<HTMLButtonElement>(null)
  const focused = useRef(false)
  // Touch ID (Settings › Security) is up for this trigger's approval.
  const prompting = useTouchIdPrompt((s) => s.key === `trigger:${tr.id}`)
  useEffect(() => {
    if (focusOnMount && !focused.current) {
      focused.current = true
      rejectRef.current?.focus({ preventScroll: true })
    }
  }, [focusOnMount])

  const trades = tr.kind !== 'alert'
  const Glyph = GLYPH[tr.kind] ?? Bell
  const balance = tr.market?.balance
  const balanceSymbol = tr.kind === 'buy' ? tr.quote.symbol : tr.token.symbol
  const facts: { key: string; label: string; value: string; tone?: 'warn' }[] = [
    { key: 'what', label: t('trading.trigger.card.fact.what'), value: actionText(tr) },
    { key: 'when', label: t('trading.trigger.card.fact.when'), value: conditionText(tr) },
  ]
  // An alert places no order: no size, no balance, no approval to wait for.
  if (trades) {
    facts.push({
      key: 'size',
      label: t('trading.trigger.card.fact.size'),
      value: sizeText(tr),
      tone: tr.action.needsApproval ? 'warn' : undefined,
    })
  }
  facts.push({
    key: 'now',
    label: t('trading.trigger.card.fact.now'),
    value: priceText(tr.market?.priceUsd ?? tr.token.priceUsd),
  })
  if (trades && balance) {
    facts.push({
      key: 'balance',
      label: t('trading.trigger.card.fact.balance'),
      value: `${formatAmount(balance.human)} ${balanceSymbol}`,
    })
  }
  facts.push(
    { key: 'wallet', label: t('trading.trigger.card.fact.wallet'), value: walletText(tr, wallets) },
    { key: 'chain', label: t('trading.trigger.card.fact.chain'), value: tr.chain.name },
    {
      key: 'validUntil',
      label: t('trading.trigger.card.fact.validUntil'),
      value: shortDate(tr.validUntil) || t('trading.trigger.card.gtc'),
    },
  )
  if (trades) {
    facts.push({
      key: 'approval',
      label: t('trading.trigger.card.fact.approval'),
      value: tr.action.needsApproval
        ? t('trading.trigger.card.waits').replace(
            '{threshold}',
            usdText(tr.action.approvalThresholdUsd),
          )
        : t('trading.trigger.card.automatic'),
      tone: tr.action.needsApproval ? 'warn' : undefined,
    })
  }
  // `expiresAt` is the proposal's deadline (24 h to decide), not the
  // trigger's life — that is "Valid until" above.
  const expires = shortDate(tr.expiresAt)
  if (expires) {
    facts.push({ key: 'expires', label: t('trading.trigger.card.fact.decideBy'), value: expires })
  }
  const notes = [...new Set([...warnings, ...triggerNotes(tr, walletName(tr, wallets))])]

  // ⌘↵ while focus is in this card (it lands on Reject when the card
  // arrives) presses Approve, arming first where a click would.
  const cardRef = useRef<HTMLElement>(null)
  useCmdEnter(() => cardRef.current)
  return (
    <article
      ref={cardRef}
      className="trd-card trd-trigger"
      data-kind="trigger"
      data-action={tr.kind}
      data-status={tr.status}
      data-testid="trigger-card"
      data-trigger={tr.id}
      aria-labelledby={`trd-trigger-${tr.id}`}
    >
      <header className="trd-card__head">
        <h3 id={`trd-trigger-${tr.id}`} className="trd-card__title">
          <Glyph className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.trigger.card.title')}
          <span className="trd-trigger__name">{tr.name}</span>
        </h3>
        <span className="trd-card__stamps">
          <span className="trd-stamp">{t(`trading.card.by.${initiatorKey(tr.initiator)}`)}</span>
          {trades && tr.action.needsApproval ? (
            <span className="trd-stamp" data-tone="warn" data-testid="trigger-needs-approval">
              {t('trading.trigger.card.needsApproval')}
            </span>
          ) : null}
        </span>
      </header>

      <p className="trd-card__legs trd-num trd-trigger__legs" data-testid="trigger-hero">
        <b>
          <Sym symbol={tr.token.symbol} />
        </b>
        <em className="trd-trigger__hero">{heroText(tr)}</em>
      </p>
      <p className="trd-trigger__now trd-mono" data-testid="trigger-now">
        {nowText(tr)}
      </p>

      <dl className="trd-card__facts">
        {facts.map((f) => (
          <div key={f.key} className="trd-card__fact" data-tone={f.tone} data-fact={f.key}>
            <dt>{f.label}</dt>
            <dd className="trd-mono trd-sym">{f.value}</dd>
          </div>
        ))}
      </dl>

      {notes.length ? (
        <ul className="trd-trigger__warnings" data-testid="trigger-warnings">
          {notes.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}

      <p className="trd-trigger__enforced" data-testid="trigger-enforced">
        {t(trades ? 'trading.trigger.card.enforced' : 'trading.trigger.card.enforcedAlert')}
      </p>

      {tr.statusReason ? (
        <section className="trd-card__note" data-testid="trigger-reason">
          <p className="trd-card__note-body">{tr.statusReason}</p>
        </section>
      ) : null}

      <div className="trd-card__actions">
        <span className="trd-card__spacer" />
        <Button
          ref={rejectRef}
          variant="secondary"
          disabled={deciding}
          onClick={() => onReject(tr)}
          data-testid="trigger-reject"
        >
          {t('trading.trigger.card.reject')}
        </Button>
        <Button
          variant="primary"
          disabled={deciding || prompting}
          onClick={() => onApprove(tr)}
          data-touch-id={prompting || undefined}
          aria-busy={prompting || undefined}
          data-testid="trigger-approve"
          data-cmd-enter
        >
          {prompting ? t('trading.touchId.prompting') : t('trading.trigger.card.approve')}
        </Button>
      </div>
    </article>
  )
}
