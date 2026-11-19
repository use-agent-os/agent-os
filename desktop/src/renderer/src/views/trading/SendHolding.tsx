import { SendHorizontal, TriangleAlert } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined } from '~/lib/biometric-gate'
import { useBalanceMask } from '~/stores/balance-privacy'
import { useSend } from '~/stores/trading'
import {
  amountFromPct,
  chainName,
  checksumAddress,
  checksumMismatch,
  compareAmounts,
  errorText,
  formatAmount,
  formatUsd,
  gasReserveEth,
  isPositiveAmount,
  maxSpendable,
  parseAmount,
  sameAddress,
  shortAddress,
  walletLabel,
} from './logic'
import { Sheet, TokenLogo } from './parts'
import type { Holding, Wallet } from './types'

const ARM_RESET_MS = 4000
const PCTS = [25, 50, 75, 100] as const

export type SendHoldingError = 'address' | 'checksum' | 'self' | 'amount' | 'insufficient' | 'gas'

/**
 * What stands between the form and a send, first problem first; null when it
 * may go. `spendable` is the balance less the gas reserve on a native row.
 */
export function checkSendHolding(input: {
  to: string
  amount: string
  from: string | null
  balance: string
  spendable: string
  decimals: number
}): SendHoldingError | null {
  const to = input.to.trim()
  if (checksumAddress(to) === null) return 'address'
  // A mixed-case address whose casing is wrong is a mangled paste.
  if (checksumMismatch(to)) return 'checksum'
  if (input.from && sameAddress(to, input.from)) return 'self'
  const amount = parseAmount(input.amount)
  if (!isPositiveAmount(amount) || amount === null) return 'amount'
  if (compareAmounts(amount, input.balance, input.decimals) > 0) return 'insufficient'
  if (compareAmounts(amount, input.spendable, input.decimals) > 0) return 'gas'
  return null
}

/**
 * Send one holding to one address, from the row it sits on. The token, the
 * chain and the paying wallet are the row's; only where and how much are
 * asked. "Send now" signs as you, immediately, so it arms first and the
 * second click confirms what is written out in full: a send cannot be undone.
 */
export function SendHolding({
  holding,
  wallets,
  fallbackWallet = null,
  onClose,
  onSent,
}: {
  holding: Holding
  wallets: readonly Wallet[]
  /** Pays when the row names no wallet of its own. */
  fallbackWallet?: string | null
  onClose: () => void
  onSent?: () => void
}) {
  const { token, chainId } = holding
  const [to, setTo] = useState('')
  const [amount, setAmount] = useState('')
  const [touched, setTouched] = useState(false)
  const [armed, setArmed] = useState(false)
  const send = useSend()
  const { mask } = useBalanceMask()
  const toId = useId()
  const amountId = useId()

  useEffect(() => {
    if (!armed) return
    const id = window.setTimeout(() => setArmed(false), ARM_RESET_MS)
    return () => window.clearTimeout(id)
  }, [armed])

  const from = holding.wallet ?? fallbackWallet
  const fromWallet = wallets.find((w) => sameAddress(w.address, from)) ?? null
  const others = wallets.filter((w) => !sameAddress(w.address, from))
  // The gas coin cannot be sent to the last wei: the send has to pay for itself.
  const reserve = token.native ? gasReserveEth(chainId) : null
  const spendable = maxSpendable(holding.amount, token.decimals, reserve)
  const parsed = parseAmount(amount)
  const usd =
    parsed !== null && holding.priceUsd !== null ? Number(parsed) * holding.priceUsd : null
  const problem = checkSendHolding({
    to,
    amount,
    from,
    balance: holding.amount,
    spendable,
    decimals: token.decimals,
  })
  const error = touched ? problem : null
  const addressError = error === 'address' || error === 'checksum' || error === 'self'
  const amountError = error === 'amount' || error === 'insufficient' || error === 'gas'
  const pending = send.isPending

  // Any edit takes back the first click: what is confirmed must be what was read.
  function edit(apply: () => void) {
    setArmed(false)
    apply()
  }

  function sendNow() {
    setTouched(true)
    if (problem !== null || parsed === null) return
    if (!armed) {
      setArmed(true)
      return
    }
    setArmed(false)
    const recipient = to.trim()
    send.mutate(
      {
        chainId,
        ...(from ? { wallet: from } : {}),
        token: token.address,
        recipients: [{ to: recipient, amount: parsed }],
        // Names the send in the Touch ID sheet (Settings › Security); never sent.
        touchId: {
          what: `${parsed} ${token.symbol} → ${shortAddress(recipient)}`,
          valueUsd: usd,
        },
      },
      {
        onSuccess: (res) => {
          const failed = res.orders.find((o) => o.status === 'failed')
          if (failed) {
            toast.error(`${t('trading.send.failed')}: ${failed.reason ?? ''}`, { id: 'trd-send' })
            return
          }
          toast.success(t('trading.send.sent'), {
            id: 'trd-send',
            description: t('trading.send.sent.body'),
          })
          onSent?.()
          onClose()
        },
        onError: (err) => {
          // Declined: the gate has said why, and nothing was sent.
          if (isTouchIdDeclined(err)) return
          toast.error(`${t('trading.send.failed')}: ${errorText(err)}`, { id: 'trd-send' })
        },
      },
    )
  }

  return (
    <Sheet
      title={`${t('trading.send.title')} ${token.symbol}`}
      // A send in flight cannot be abandoned: Escape, the backdrop and the X
      // all wait for the answer.
      onClose={pending ? () => {} : onClose}
      foot={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            {t('trading.sheet.cancel')}
          </Button>
          <Button
            variant="primary"
            disabled={pending}
            onClick={sendNow}
            data-armed={armed || undefined}
            title={t('trading.send.now.help')}
            data-testid="holding-send-now"
          >
            <SendHorizontal className="size-3.5" strokeWidth={2} aria-hidden />
            {pending
              ? t('trading.send.sending')
              : armed
                ? t('trading.send.now.confirm')
                : t('trading.send.now')}
          </Button>
        </>
      }
    >
      <div className="trd-xfer" data-testid="holding-send">
        <div className="trd-xfer__asset">
          <TokenLogo token={token} size={28} />
          <div className="trd-xfer__names">
            <b>{token.symbol}</b>
            <span>{chainName(chainId)}</span>
          </div>
          <div className="trd-xfer__from">
            <span>{t('trading.send.wallet')}</span>
            <b title={from ?? undefined}>
              {fromWallet ? walletLabel(fromWallet) : from ? shortAddress(from) : '—'}
            </b>
          </div>
        </div>

        <div className="trd-xfer__field" data-error={addressError || undefined}>
          <label htmlFor={toId}>{t('trading.send.to')}</label>
          <input
            id={toId}
            className="mac-input trd-mono"
            value={to}
            placeholder="0x…"
            autoFocus
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => edit(() => setTo(e.target.value))}
            data-testid="holding-send-to"
          />
          {others.length ? (
            <div className="trd-xfer__mine" aria-label={t('trading.send.mine')}>
              <span>{t('trading.send.mine')}</span>
              {others.map((w) => (
                <button
                  key={w.address}
                  type="button"
                  className="trd-chip app-no-drag"
                  aria-pressed={sameAddress(w.address, to.trim())}
                  title={w.address}
                  onClick={() => edit(() => setTo(checksumAddress(w.address) ?? w.address))}
                  data-testid="holding-send-own"
                >
                  {walletLabel(w)}
                </button>
              ))}
            </div>
          ) : null}
        </div>

        <div className="trd-xfer__field" data-error={amountError || undefined}>
          <div className="trd-xfer__row">
            <label htmlFor={amountId}>{t('trading.holdings.col.amount')}</label>
            <span className="trd-xfer__balance" data-testid="holding-send-balance">
              {t('trading.send.balance')} {mask(formatAmount(holding.amount))} {token.symbol}
            </span>
          </div>
          <div className="trd-xfer__amount">
            <input
              id={amountId}
              className="mac-input trd-num"
              inputMode="decimal"
              value={amount}
              placeholder="0"
              autoComplete="off"
              onChange={(e) => edit(() => setAmount(e.target.value))}
              data-testid="holding-send-amount"
            />
            <span>{token.symbol}</span>
          </div>
          <div className="trd-xfer__row">
            <span className="trd-pcts" aria-label={t('trading.holdings.col.amount')}>
              {PCTS.map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() =>
                    edit(() =>
                      setAmount(
                        p === 100 ? spendable : amountFromPct(spendable, token.decimals, p),
                      ),
                    )
                  }
                  data-testid={`holding-send-pct-${p}`}
                >
                  {p === 100 ? t('trading.swap.max') : `${p}%`}
                </button>
              ))}
            </span>
            <span className="trd-leg__usd">{usd !== null ? `≈ ${formatUsd(usd)}` : ''}</span>
          </div>
          {reserve ? (
            <small data-testid="holding-send-reserve">
              {t('trading.swap.gasReserve')} ~{formatAmount(reserve)} {token.symbol}{' '}
              {t('trading.swap.gasReserve.for')}
            </small>
          ) : null}
        </div>

        {error ? (
          <p className="trd-xfer__error" role="alert" data-testid="holding-send-error">
            {t(`trading.send.one.error.${error}`)}
          </p>
        ) : null}

        {/* Armed: the second click is a confirmation, so what it confirms is
            written out in full — the whole address, the exact amount. */}
        {armed && problem === null && parsed !== null ? (
          <section
            className="trd-xfer__review"
            data-testid="holding-send-review"
            aria-live="polite"
          >
            <h4>{t('trading.send.review.title')}</h4>
            <b className="trd-num">
              {parsed} {token.symbol}
              {usd !== null ? <span> · {formatUsd(usd)}</span> : null}
            </b>
            <code className="trd-mono">{to.trim()}</code>
          </section>
        ) : null}

        <p className="trd-xfer__warn">
          <TriangleAlert className="size-3" strokeWidth={2} aria-hidden />
          {t('trading.send.one.irreversible')}
        </p>
      </div>
    </Sheet>
  )
}
