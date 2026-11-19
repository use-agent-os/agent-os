import { ArrowDown, RefreshCw, TriangleAlert } from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined } from '~/lib/biometric-gate'
import { useNow } from '~/lib/use-now'
import { useSwap } from '~/stores/trading'
import {
  errorCode,
  errorText,
  formatAmount,
  formatPct,
  formatUsd,
  CONFIRM_WORD,
  confirmWordMatches,
  impactTone,
  needsRetype,
  PRICE_MOVED,
  quoteCountdown,
  toRaw,
  walletLabel,
} from './logic'
import { Sheet } from './parts'
import { DexFeeFact, QuoteWarnings } from './SwapPanel'
import { providerLabel, type Order, type Quote, type Token, type Wallet } from './types'

const timeFmt = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

/**
 * The only door a swap leaves through. Restates both legs, the wallet, the
 * facts; above 1,000 USD it asks for "confirm" to be typed; a price
 * older than its window must be refreshed before Swap now enables.
 *
 * The quote is frozen when the sheet opens: the panel behind keeps
 * re-quoting every 15 s, and the numbers a person reads must be the ones
 * they confirm. "Refresh quote" (and the stale-price refresh) adopt
 * the next answer; nothing else changes them.
 */
export function ConfirmSwap({
  quote: liveQuote,
  fetchedAt: liveFetchedAt,
  wallet,
  tokenIn,
  tokenOut,
  amount,
  slippagePct,
  refreshing,
  quoteError = null,
  onRefresh,
  onClose,
  onSent,
}: {
  quote: Quote
  fetchedAt: number
  wallet: Wallet
  tokenIn: Token
  tokenOut: Token
  amount: string
  slippagePct: number | undefined
  refreshing: boolean
  /** Why the last refresh failed, so a dead Refresh button is never a mystery. */
  quoteError?: string | null
  onRefresh: () => void
  onClose: () => void
  onSent: (orders: Order[]) => void
}) {
  const now = useNow(1000)
  const [frozen, setFrozen] = useState({ quote: liveQuote, fetchedAt: liveFetchedAt })
  // A refresh was asked for: the next live quote that lands is adopted
  // (state adjusted during render, as React prescribes for derived state).
  const [adopting, setAdopting] = useState(false)
  if (
    adopting &&
    !refreshing &&
    (liveQuote !== frozen.quote || liveFetchedAt !== frozen.fetchedAt)
  ) {
    setFrozen({ quote: liveQuote, fetchedAt: liveFetchedAt })
    setAdopting(false)
  }
  function refresh() {
    setAdopting(true)
    onRefresh()
  }
  const quote = frozen.quote
  const fetchedAt = frozen.fetchedAt
  const stale = quoteCountdown(fetchedAt, now, quote.expiresAt).expired
  const retype = needsRetype(quote.valueUsd)
  const [typed, setTyped] = useState('')
  const retypeOk = !retype || confirmWordMatches(typed)
  const swap = useSwap()
  const retypeId = useId()
  const impact = impactTone(quote.priceImpactPct)

  function send() {
    // The numbers the person read and confirmed are the ones the
    // engine is held to: it re-quotes on send and refuses to fill at a
    // worse price than this frozen quote promised.
    const expectedOutRaw =
      quote.amountOutRaw ?? toRaw(quote.amountOut, tokenOut.decimals).toString()
    const minOutRaw = quote.minOutRaw ?? toRaw(quote.minOut, tokenOut.decimals).toString()
    swap.mutate(
      {
        chainId: quote.chainId,
        wallets: [wallet.address],
        tokenIn: tokenIn.address,
        tokenOut: tokenOut.address,
        amountIn: amount,
        ...(slippagePct !== undefined ? { slippagePct } : {}),
        expectedOutRaw,
        minOutRaw,
        ...(quote.quoteId ? { quoteId: quote.quoteId } : {}),
        // Names the swap in the Touch ID sheet (Settings › Security); never sent.
        touchId: {
          what: `${amount} ${tokenIn.symbol} → ${tokenOut.symbol}`,
          valueUsd: quote.valueUsd,
        },
      },
      {
        onSuccess: (res) => {
          const orders = res?.orders ?? []
          const failed = orders.find((o) => o.status === 'failed' || o.status === 'rejected')
          if (failed) {
            toast.error(`${t('trading.swap.failed')}: ${failed.reason ?? ''}`, { id: 'trd-swap' })
          } else {
            toast.success(t('trading.swap.sent'), {
              id: 'trd-swap',
              description: t('trading.swap.sent.body'),
            })
          }
          onSent(orders)
        },
        onError: (err) => {
          // Declined: the gate has said why, and nothing was sent.
          if (isTouchIdDeclined(err)) return
          if (errorCode(err) === PRICE_MOVED) {
            // Nothing was sent. The sheet stays open on a fresh price so the
            // person can read the new numbers and decide again.
            toast.warning(t('trading.confirm.priceMoved'), { id: 'trd-swap' })
            refresh()
            return
          }
          toast.error(`${t('trading.swap.error')}: ${errorText(err)}`, { id: 'trd-swap' })
        },
      },
    )
  }

  return (
    <Sheet
      title={t('trading.confirm.title')}
      role="alertdialog"
      onClose={swap.isPending ? () => {} : onClose}
      note={
        <>
          {t('trading.confirm.from')} <b>{walletLabel(wallet)}</b>
        </>
      }
      foot={
        <>
          <Button disabled={swap.isPending} onClick={onClose}>
            {t('trading.confirm.cancel')}
          </Button>
          {stale ? (
            <Button
              variant="primary"
              disabled={refreshing}
              onClick={refresh}
              data-testid="confirm-refresh"
            >
              {t('trading.swap.requote')}
            </Button>
          ) : (
            <Button
              variant="primary"
              disabled={swap.isPending || !retypeOk}
              onClick={send}
              data-testid="confirm-send"
            >
              {swap.isPending ? t('trading.confirm.sending') : t('trading.confirm.cta')}
            </Button>
          )}
        </>
      }
    >
      <div className="trd-confirm__legs">
        <div className="trd-confirm__leg">
          <small>{t('trading.confirm.pay')}</small>
          <b>
            {formatAmount(amount)}
            <span>{tokenIn.symbol}</span>
          </b>
        </div>
        <ArrowDown className="trd-confirm__arrow size-3.5" strokeWidth={2} aria-hidden />
        <div className="trd-confirm__leg">
          <small>{t('trading.confirm.receive')}</small>
          <b>
            {formatAmount(quote.amountOut)}
            <span>{tokenOut.symbol}</span>
          </b>
        </div>
        <span className="trd-confirm__min">
          {t('trading.confirm.atLeast')} {formatAmount(quote.minOut)} {tokenOut.symbol}
        </span>
      </div>

      <div className="trd-facts">
        <div className="trd-fact">
          <span>{t('trading.orders.value')}</span>
          <b>{formatUsd(quote.valueUsd)}</b>
        </div>
        <div className="trd-fact">
          <span>
            {t('trading.swap.impact')}{' '}
            {quote.provider ? (
              <span className="text-dim">
                {t('trading.provider.via')} {providerLabel(quote.provider)}
              </span>
            ) : null}
          </span>
          <b data-tone={impact}>{formatPct(quote.priceImpactPct)}</b>
        </div>
        {quote.swapFee ? <DexFeeFact fee={quote.swapFee} /> : null}
        <div className="trd-fact">
          <span>{t('trading.swap.gas')}</span>
          <b>{formatUsd(quote.gasUsd)}</b>
        </div>
        <div className="trd-fact">
          <span>{t('trading.swap.slippage')}</span>
          <b>{formatPct(quote.slippagePct)}</b>
        </div>
        <div className="trd-fact">
          <span>{t('trading.confirm.quoteAsOf')}</span>
          <b>
            {fetchedAt ? timeFmt.format(fetchedAt) : '–'}
            <Button
              variant="ghost"
              className="ml-2"
              disabled={refreshing || swap.isPending}
              onClick={refresh}
              aria-label={t('trading.confirm.refreshQuote')}
              title={t('trading.confirm.refreshQuote')}
              data-testid="confirm-refresh-quote"
            >
              <RefreshCw
                className={refreshing ? 'size-3 animate-spin' : 'size-3'}
                strokeWidth={1.75}
                aria-hidden
              />
              {t('trading.confirm.refreshQuote')}
            </Button>
          </b>
        </div>
      </div>

      {impact === 'danger' ? (
        <div className="trd-warn" data-tone="warn" role="status">
          <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
          <span>{t('trading.confirm.impactHigh')}</span>
        </div>
      ) : null}
      {stale ? (
        <div className="trd-warn" data-tone="warn" role="status" data-testid="confirm-stale">
          <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
          <span>{t('trading.confirm.stale')}</span>
        </div>
      ) : null}
      {quoteError ? (
        <div className="trd-warn" data-tone="danger" role="alert" data-testid="confirm-quote-error">
          <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
          <span>
            {t('trading.confirm.refreshFailed')}: {quoteError}
          </span>
        </div>
      ) : null}

      <QuoteWarnings warnings={quote.warnings} />

      {retype ? (
        <div className="trd-field">
          <label htmlFor={retypeId}>
            {t('trading.confirm.retype').replace('{word}', CONFIRM_WORD)}
          </label>
          <input
            id={retypeId}
            className="mac-input"
            data-mono="true"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            placeholder={CONFIRM_WORD}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            data-testid="confirm-retype"
          />
        </div>
      ) : null}
    </Sheet>
  )
}
