import { ArrowDown, RefreshCw, TriangleAlert } from 'lucide-react'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined } from '~/lib/biometric-gate'
import { useNow } from '~/lib/use-now'
import { useBridge } from '~/stores/trading'
import { formatEta } from './bridge-logic'
import { ChainBadge } from './ChainMark'
import {
  CONFIRM_WORD,
  confirmWordMatches,
  errorCode,
  errorText,
  formatAmount,
  formatUsd,
  needsRetype,
  PRICE_MOVED,
  quoteCountdown,
  walletLabel,
} from './logic'
import { Sheet } from './parts'
import { providerLabel, type BridgeQuote, type Order, type Token, type Wallet } from './types'

const timeFmt = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

/**
 * The only door a manual bridge leaves through: the swap's confirm sheet,
 * with a chain on each leg. The quote is frozen when the sheet opens and
 * only a refresh replaces it. The engine asks the same bridge again before
 * it signs and holds that answer to its own 1% floor (docs/bridge.md).
 */
export function ConfirmBridge({
  quote: liveQuote,
  fetchedAt: liveFetchedAt,
  wallet,
  tokenIn,
  tokenOut,
  amount,
  refreshing,
  quoteError = null,
  onRefresh,
  onClose,
  onSent,
}: {
  quote: BridgeQuote
  fetchedAt: number
  wallet: Wallet
  tokenIn: Token
  tokenOut: Token
  amount: string
  refreshing: boolean
  quoteError?: string | null
  onRefresh: () => void
  onClose: () => void
  onSent: (orders: Order[]) => void
}) {
  const now = useNow(1000)
  const [frozen, setFrozen] = useState({ quote: liveQuote, fetchedAt: liveFetchedAt })
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
  const stale = quoteCountdown(fetchedAt, now).expired
  const retype = needsRetype(quote.valueUsd)
  const [typed, setTyped] = useState('')
  const retypeOk = !retype || confirmWordMatches(typed)
  const bridge = useBridge()
  const retypeId = useId()
  const eta = formatEta(quote.timeEstimateSeconds)

  function send() {
    bridge.mutate(
      {
        chainId: quote.originChainId,
        toChainId: quote.destinationChainId,
        token: tokenIn.address,
        amount,
        wallet: wallet.address,
        // Names the bridge in the Touch ID sheet (Settings › Security); never sent.
        touchId: {
          what: `${amount} ${tokenIn.symbol} → ${tokenOut.symbol}`,
          valueUsd: quote.valueUsd,
        },
      },
      {
        onSuccess: (res) => {
          const order = res?.order
          if (order && (order.status === 'failed' || order.status === 'rejected')) {
            toast.error(`${t('trading.bridge.failed')}: ${order.reason ?? ''}`, {
              id: 'trd-bridge',
            })
          } else {
            toast.success(t('trading.bridge.sent'), {
              id: 'trd-bridge',
              description: t('trading.bridge.sent.body'),
            })
          }
          onSent(order ? [order] : [])
        },
        onError: (err) => {
          // Declined: the gate has said why, and nothing was sent.
          if (isTouchIdDeclined(err)) return
          if (errorCode(err) === PRICE_MOVED) {
            toast.warning(t('trading.confirm.priceMoved'), { id: 'trd-bridge' })
            refresh()
            return
          }
          toast.error(`${t('trading.bridge.error')}: ${errorText(err)}`, { id: 'trd-bridge' })
        },
      },
    )
  }

  return (
    <Sheet
      title={t('trading.bridge.confirm.title')}
      role="alertdialog"
      onClose={bridge.isPending ? () => {} : onClose}
      note={
        <>
          {t('trading.confirm.from')} <b>{walletLabel(wallet)}</b>
        </>
      }
      foot={
        <>
          <Button disabled={bridge.isPending} onClick={onClose}>
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
              disabled={bridge.isPending || !retypeOk}
              onClick={send}
              data-testid="confirm-send"
            >
              {bridge.isPending ? t('trading.confirm.sending') : t('trading.bridge.confirm.cta')}
            </Button>
          )}
        </>
      }
    >
      <div className="trd-confirm__legs">
        <div className="trd-confirm__leg">
          <small>
            {t('trading.confirm.pay')} <ChainBadge chainId={quote.originChainId} />
          </small>
          <b>
            {formatAmount(amount)}
            <span>{tokenIn.symbol}</span>
          </b>
        </div>
        <ArrowDown className="trd-confirm__arrow size-3.5" strokeWidth={2} aria-hidden />
        <div className="trd-confirm__leg">
          <small>
            {t('trading.confirm.receive')} <ChainBadge chainId={quote.destinationChainId} />
          </small>
          <b>
            {formatAmount(quote.expectedOut)}
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
            {t('trading.bridge.fees')}{' '}
            <span className="text-dim">
              {t('trading.provider.via')} {providerLabel(quote.provider ?? 'relay')}
            </span>
          </span>
          <b>{formatUsd(quote.feesUsd)}</b>
        </div>
        {eta ? (
          <div className="trd-fact">
            <span>{t('trading.bridge.eta')}</span>
            <b>{eta}</b>
          </div>
        ) : null}
        <div className="trd-fact">
          <span>{t('trading.confirm.quoteAsOf')}</span>
          <b>
            {fetchedAt ? timeFmt.format(fetchedAt) : '–'}
            <Button
              variant="ghost"
              className="ml-2"
              disabled={refreshing || bridge.isPending}
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

      {quote.refundGuaranteed === false ? (
        <div className="trd-warn" data-tone="warn" role="status" data-testid="confirm-refund-warn">
          <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
          <span>{t('trading.bridge.refundNotGuaranteed')}</span>
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

      {retype ? (
        <div className="trd-field">
          <label htmlFor={retypeId}>
            {t('trading.bridge.confirm.retype').replace('{word}', CONFIRM_WORD)}
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
