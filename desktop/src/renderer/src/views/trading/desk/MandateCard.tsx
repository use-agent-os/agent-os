import { CalendarClock } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useTouchIdPrompt } from '~/lib/biometric-gate'
import { initiatorKey, sameAddress, shortAddress, walletLabel } from '../logic'
import { Sym } from '../parts'
import type { Mandate, Wallet } from '../types'
import { everyAfter, everyPhrase, usdShort } from './mandate-logic'
import { useCmdEnter } from '../cmd-enter'

/** "Sep 29 02:42": short enough to sit whole beside its label. */
function expiry(iso: string | null): string {
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

function walletText(m: Mandate, wallets: readonly Wallet[]): string {
  const known = wallets.find((w) => sameAddress(w.address, m.wallet.address))
  if (known) return `${walletLabel(known)} · ${shortAddress(known.address)}`
  return m.wallet.label
    ? `${m.wallet.label} · ${shortAddress(m.wallet.address)}`
    : shortAddress(m.wallet.address)
}

/**
 * A DCA the agent proposed, waiting for the operator (docs/dca.md). One
 * decision covers every buy after it, so the card says the whole of it —
 * what, how much, how often, until what, from which wallet — before the one
 * button that starts it. The engine enforces all of it; the card only reads.
 */
export function MandateCard({
  mandate: m,
  wallets,
  deciding,
  onApprove,
  onReject,
  focusOnMount = false,
  warnings = [],
}: {
  mandate: Mandate
  wallets: readonly Wallet[]
  /** The engine's own warnings for this mandate, when the caller has them. */
  warnings?: readonly string[]
  deciding: boolean
  onApprove: (m: Mandate) => void
  onReject: (m: Mandate) => void
  focusOnMount?: boolean
}) {
  const rejectRef = useRef<HTMLButtonElement>(null)
  const focused = useRef(false)
  // Touch ID (Settings › Security) is up for this mandate's approval.
  const prompting = useTouchIdPrompt((s) => s.key === `mandate:${m.id}`)
  useEffect(() => {
    if (focusOnMount && !focused.current) {
      focused.current = true
      rejectRef.current?.focus({ preventScroll: true })
    }
  }, [focusOnMount])

  const every = everyPhrase(m.schedule.everySeconds)
  const facts: { key: string; label: string; value: string; tone?: 'warn' }[] = [
    {
      key: 'buy',
      label: t('trading.dca.card.fact.buy'),
      value:
        `${usdShort(m.budget.usdPerRun)} ${m.quote.symbol ? `of ${m.quote.symbol}` : ''}`.trim(),
      tone: m.guards.buysNeedApproval ? 'warn' : undefined,
    },
    { key: 'every', label: t('trading.dca.card.fact.every'), value: m.schedule.label || every },
    { key: 'cap', label: t('trading.dca.card.fact.cap'), value: usdShort(m.budget.capUsd) },
    {
      key: 'buys',
      label: t('trading.dca.card.fact.buys'),
      value: m.runs.max !== null ? String(m.runs.max) : t('trading.dca.card.untilCap'),
    },
  ]
  if (m.guards.maxPriceUsd !== null) {
    facts.push({
      key: 'maxPrice',
      label: t('trading.dca.card.fact.maxPrice'),
      value: usdShort(m.guards.maxPriceUsd),
    })
  }
  facts.push(
    { key: 'wallet', label: t('trading.dca.card.fact.wallet'), value: walletText(m, wallets) },
    { key: 'chain', label: t('trading.dca.card.fact.chain'), value: m.chain.name },
    {
      key: 'first',
      label: t('trading.dca.card.fact.first'),
      value: m.schedule.startNow
        ? t('trading.dca.card.firstNow')
        : t('trading.dca.card.firstNext').replace('{every}', everyAfter(m.schedule.everySeconds)),
    },
  )
  // What the engine would warn about, read from the mandate itself: the list
  // payload carries warnings for the whole list, not per mandate.
  const notes = [...warnings]
  if (m.guards.buysNeedApproval) {
    notes.push(
      t('trading.dca.contract.overThreshold')
        .replace('{usd}', usdShort(m.budget.usdPerRun))
        .replace('{threshold}', usdShort(m.guards.approvalThresholdUsd)),
    )
  }
  if (m.guards.maxPriceUsd !== null && m.token.priceUsd === null) {
    notes.push(t('trading.dca.card.noPrice'))
  }
  // `expiresAt` is the proposal's deadline (24 h to decide), not the DCA's end.
  const expires = expiry(m.expiresAt)
  if (expires) {
    facts.push({ key: 'expires', label: t('trading.dca.card.fact.decideBy'), value: expires })
  }

  // ⌘↵ while focus is in this card (it lands on Reject when the card
  // arrives) presses Approve, arming first where a click would.
  const cardRef = useRef<HTMLElement>(null)
  useCmdEnter(() => cardRef.current)
  return (
    <article
      ref={cardRef}
      className="trd-card trd-mandate"
      data-kind="dca"
      data-status={m.status}
      data-testid="mandate-card"
      data-mandate={m.id}
      aria-labelledby={`trd-mandate-${m.id}`}
    >
      <header className="trd-card__head">
        <h3 id={`trd-mandate-${m.id}`} className="trd-card__title">
          <CalendarClock className="size-3.5" strokeWidth={1.75} aria-hidden />
          {t('trading.dca.card.title')}
        </h3>
        <span className="trd-card__stamps">
          <span className="trd-stamp">{t(`trading.card.by.${initiatorKey(m.initiator)}`)}</span>
          {m.guards.buysNeedApproval ? (
            <span className="trd-stamp" data-tone="warn" data-testid="mandate-needs-approval">
              {t('trading.dca.card.needsApproval')}
            </span>
          ) : null}
        </span>
      </header>

      <p className="trd-card__legs trd-num trd-mandate__legs" data-testid="mandate-legs">
        <b>
          <Sym symbol={m.token.symbol} />
        </b>
        <span aria-hidden>←</span>
        <b>
          <Sym symbol={m.quote.symbol} />
        </b>
        <em className="trd-mandate__rate">
          {usdShort(m.budget.usdPerRun)} {every}
        </em>
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
        <ul className="trd-mandate__warnings" data-testid="mandate-warnings">
          {[...new Set(notes)].map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}

      <p className="trd-mandate__enforced">{t('trading.dca.card.enforced')}</p>

      {m.statusReason ? (
        <section className="trd-card__note" data-testid="mandate-reason">
          <p className="trd-card__note-body">{m.statusReason}</p>
        </section>
      ) : null}

      <div className="trd-card__actions">
        <span className="trd-card__spacer" />
        <Button
          ref={rejectRef}
          variant="secondary"
          disabled={deciding}
          onClick={() => onReject(m)}
          data-testid="mandate-reject"
        >
          {t('trading.dca.card.reject')}
        </Button>
        <Button
          variant="primary"
          disabled={deciding || prompting}
          onClick={() => onApprove(m)}
          data-touch-id={prompting || undefined}
          aria-busy={prompting || undefined}
          data-testid="mandate-approve"
          data-cmd-enter
        >
          {prompting ? t('trading.touchId.prompting') : t('trading.dca.card.approve')}
        </Button>
      </div>
    </article>
  )
}
