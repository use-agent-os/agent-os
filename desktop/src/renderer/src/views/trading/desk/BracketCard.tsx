import { Bell, ChevronsUpDown } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useTouchIdPrompt } from '~/lib/biometric-gate'
import { formatAmount, initiatorKey, sameAddress, shortAddress, walletLabel } from '../logic'
import { Sym } from '../parts'
import type { Bracket, Wallet } from '../types'
import {
  bracketHeroText,
  bracketNotes,
  bracketSizeText,
  movesText,
  rewardRiskText,
} from './bracket-logic'
import { priceText, usdText } from './trigger-logic'
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

function walletName(b: Bracket, wallets: readonly Wallet[]): string {
  const known = wallets.find((w) => sameAddress(w.address, b.wallet.address))
  if (known) return walletLabel(known)
  return b.wallet.label || shortAddress(b.wallet.address)
}

function walletText(b: Bracket, wallets: readonly Wallet[]): string {
  const known = wallets.find((w) => sameAddress(w.address, b.wallet.address))
  if (known) return `${walletLabel(known)} · ${shortAddress(known.address)}`
  return b.wallet.label
    ? `${b.wallet.label} · ${shortAddress(b.wallet.address)}`
    : shortAddress(b.wallet.address)
}

/** "ETH $3,800 · +20 % / −10 % · awaiting approval": the live line under the sentence. */
function nowLine(b: Bracket): string {
  const price = b.market?.priceUsd ?? b.token.priceUsd
  const state = t('trading.trigger.state.awaiting').toLowerCase()
  if (price === null || price === undefined) return state
  return [`${b.token.symbol} ${priceText(price)}`, movesText(b), state].filter(Boolean).join(' · ')
}

/**
 * A bracket the agent proposed, waiting for the operator (docs/brackets.md):
 * a take-profit and a stop-loss on one position. One decision arms both legs,
 * and either may trade later with nobody watching, so the card says the whole
 * of it — both lines, the size, the wallet, until when — before the one
 * button that arms it. The engine enforces all of it; the card only reads.
 * It is a trigger card in the same family (`trd-trigger`), keyed `bracket`.
 */
export function BracketCard({
  bracket: b,
  wallets,
  deciding,
  onApprove,
  onReject,
  focusOnMount = false,
  warnings = [],
}: {
  bracket: Bracket
  wallets: readonly Wallet[]
  /** The engine's own warnings for this bracket, when the caller has them. */
  warnings?: readonly string[]
  deciding: boolean
  onApprove: (b: Bracket) => void
  onReject: (b: Bracket) => void
  focusOnMount?: boolean
}) {
  const rejectRef = useRef<HTMLButtonElement>(null)
  const focused = useRef(false)
  // Touch ID (Settings › Security) is up for this bracket's approval.
  const prompting = useTouchIdPrompt((s) => s.key === `bracket:${b.id}`)
  useEffect(() => {
    if (focusOnMount && !focused.current) {
      focused.current = true
      rejectRef.current?.focus({ preventScroll: true })
    }
  }, [focusOnMount])

  const trades = b.kind !== 'alert'
  const Glyph = trades ? ChevronsUpDown : Bell
  const balance = b.market?.balance
  // The sentence above already says what it does and both lines, and the
  // live line under it the price now: the table holds only what neither says.
  // At a 1200×800 window the repeated rows pushed the card past the region's
  // room and cut its explanation off mid-sentence.
  const facts: { key: string; label: string; value: string; tone?: 'warn' }[] = []
  if (trades) {
    facts.push({
      key: 'size',
      label: t('trading.trigger.card.fact.size'),
      value: bracketSizeText(b),
      tone: b.action.needsApproval ? 'warn' : undefined,
    })
  }
  const rr = rewardRiskText(b)
  if (trades && rr) {
    facts.push({ key: 'rewardRisk', label: t('trading.bracket.card.fact.rewardRisk'), value: rr })
  }
  if (trades && balance) {
    facts.push({
      key: 'balance',
      label: t('trading.trigger.card.fact.balance'),
      value: `${formatAmount(balance.human)} ${b.token.symbol}`,
    })
  }
  facts.push(
    { key: 'wallet', label: t('trading.trigger.card.fact.wallet'), value: walletText(b, wallets) },
    { key: 'chain', label: t('trading.trigger.card.fact.chain'), value: b.chain.name },
    {
      key: 'validUntil',
      label: t('trading.trigger.card.fact.validUntil'),
      value: shortDate(b.validUntil) || t('trading.trigger.card.gtc'),
    },
  )
  if (trades) {
    facts.push({
      key: 'approval',
      label: t('trading.trigger.card.fact.approval'),
      value: b.action.needsApproval
        ? t('trading.trigger.card.waits').replace(
            '{threshold}',
            usdText(b.action.approvalThresholdUsd),
          )
        : t('trading.trigger.card.automatic'),
      tone: b.action.needsApproval ? 'warn' : undefined,
    })
  }
  // The proposal's deadline (24 h to decide), not the bracket's life.
  const expires = shortDate(b.expiresAt)
  if (expires) {
    facts.push({ key: 'expires', label: t('trading.trigger.card.fact.decideBy'), value: expires })
  }
  const notes = [...new Set([...warnings, ...bracketNotes(b, walletName(b, wallets))])]
  const enforced = t(
    trades ? 'trading.bracket.card.enforced' : 'trading.bracket.card.enforcedAlert',
  )

  // ⌘↵ while focus is in this card (it lands on Reject when the card
  // arrives) presses Approve, arming first where a click would.
  const cardRef = useRef<HTMLElement>(null)
  useCmdEnter(() => cardRef.current)
  return (
    <article
      ref={cardRef}
      className="trd-card trd-trigger trd-bracket"
      data-kind="bracket"
      data-action={b.kind}
      data-status={b.status}
      data-testid="bracket-card"
      data-bracket={b.id}
      aria-labelledby={`trd-bracket-${b.id}`}
    >
      <header className="trd-card__head">
        <h3 id={`trd-bracket-${b.id}`} className="trd-card__title">
          <Glyph className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.bracket.card.title')}
          <span className="trd-trigger__name">{b.name}</span>
        </h3>
        <span className="trd-card__stamps">
          <span className="trd-stamp">{t(`trading.card.by.${initiatorKey(b.initiator)}`)}</span>
          <span className="trd-stamp" data-testid="bracket-both-legs">
            {t('trading.bracket.card.bothLegs')}
          </span>
          {trades && b.action.needsApproval ? (
            <span className="trd-stamp" data-tone="warn" data-testid="bracket-needs-approval">
              {t('trading.trigger.card.needsApproval')}
            </span>
          ) : null}
        </span>
      </header>

      <p className="trd-card__legs trd-num trd-trigger__legs" data-testid="bracket-hero">
        <b>
          <Sym symbol={b.token.symbol} />
        </b>
        <em className="trd-trigger__hero">{bracketHeroText(b)}</em>
      </p>
      <p className="trd-trigger__now trd-mono" data-testid="bracket-now">
        {nowLine(b)}
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
        <ul className="trd-trigger__warnings" data-testid="bracket-warnings">
          {notes.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}

      {/* One line; the whole sentence is its tooltip and its accessible text. */}
      <p className="trd-trigger__enforced" data-testid="bracket-enforced" title={enforced}>
        {enforced}
      </p>

      {b.statusReason ? (
        <section className="trd-card__note" data-testid="bracket-reason">
          <p className="trd-card__note-body">{b.statusReason}</p>
        </section>
      ) : null}

      <div className="trd-card__actions">
        <span className="trd-card__spacer" />
        <Button
          ref={rejectRef}
          variant="secondary"
          disabled={deciding}
          onClick={() => onReject(b)}
          data-testid="bracket-reject"
        >
          {t('trading.trigger.card.reject')}
        </Button>
        <Button
          variant="primary"
          disabled={deciding || prompting}
          onClick={() => onApprove(b)}
          data-touch-id={prompting || undefined}
          aria-busy={prompting || undefined}
          data-testid="bracket-approve"
          data-cmd-enter
        >
          {prompting ? t('trading.touchId.prompting') : t('trading.trigger.card.approve')}
        </Button>
      </div>
    </article>
  )
}
