import { ArrowDownUp, ChevronDown, CircleCheck, ShieldAlert, TriangleAlert } from 'lucide-react'
import { useMemo, useState, type CSSProperties } from 'react'
import { t, type MessageKey } from '~/i18n'
import { useNow } from '~/lib/use-now'
import { useBalances, useBridgeQuote, type BridgeParams } from '~/stores/trading'
import { BRIDGE_ASSETS, bridgeToken, formatEta, otherChain, type BridgeAsset } from './bridge-logic'
import { ConfirmBridge } from './ConfirmBridge'
import {
  activeFirst,
  amountFromPct,
  chainShort,
  compareAmounts,
  formatAmount,
  formatUsd,
  gasReserveEth,
  isPositiveAmount,
  maxSpendable,
  parseAmount,
  quoteCountdown,
  sameAddress,
  sameToken,
  walletLabel,
} from './logic'
import { TokenLogo } from './parts'
import { Leg, useAmountFit } from './SwapPanel'
import { TicketModes, type TicketMode } from './TicketModes'
import {
  CHAINS,
  providerLabel,
  type Balance,
  type BridgeQuote,
  type ChainId,
  type Order,
  type Token,
  type Wallet,
} from './types'

/**
 * The ticket, bridging: the same asset from one chain to the other through
 * the AgentOS DEX (docs/bridge.md). It is the swap ticket's twin on purpose: one
 * wallet, the chain it pays on in the head, two legs, a live price on the
 * button, and a confirm sheet as the only door out. What differs is what a
 * bridge is: the receive leg is never chosen (ETH lands as ETH, USDC as
 * USDG), and each leg names its chain.
 */
export function BridgePanel({
  wallets,
  primary,
  selectedWallet,
  unlocked,
  onSent,
  onMode,
}: {
  wallets: Wallet[]
  primary: string | null
  /** The rail's selection: an address, or 'all'. */
  selectedWallet: string
  unlocked: boolean
  onSent: (orders: Order[]) => void
  /** The full desk's ticket also swaps: its head then switches Swap | Bridge. */
  onMode?: (mode: TicketMode) => void
}) {
  const [chainId, setChainId] = useState<ChainId>(8453)
  const [asset, setAsset] = useState<BridgeAsset>('ETH')
  const [amount, setAmount] = useState('')
  const [confirm, setConfirm] = useState(false)
  const now = useNow(1000)
  const toChainId = otherChain(chainId)

  // As on the swap ticket: the rail's choice follows in ("all" means the
  // primary) until the ticket's own menu overrides it.
  const [walletChoice, setWalletChoice] = useState<{ base: string; override: string | null }>({
    base: selectedWallet,
    override: null,
  })
  if (walletChoice.base !== selectedWallet) {
    setWalletChoice({ base: selectedWallet, override: null })
  }
  const followed =
    selectedWallet !== 'all' ? selectedWallet : (primary ?? wallets[0]?.address ?? '')
  const override =
    walletChoice.override && wallets.some((w) => sameAddress(w.address, walletChoice.override))
      ? walletChoice.override
      : null
  const wallet = override ?? followed

  function pickChain(next: ChainId) {
    if (next === chainId) return
    setChainId(next)
    setAmount('')
  }

  const parsed = parseAmount(amount)
  const { balances } = useBalances(wallet || undefined, Boolean(wallet))
  // The ledger's own token rows carry the art; the table only names the route.
  const held = (token: Token): Balance | null =>
    balances.find((b) => b.chainId === token.chainId && sameToken(b.token, token)) ?? null
  const routeIn = bridgeToken(chainId, asset)
  const routeOut = bridgeToken(toChainId, asset)
  const available = useMemo(
    () => balances.find((b) => b.chainId === chainId && sameToken(b.token, routeIn)) ?? null,
    [balances, chainId, routeIn],
  )
  const tokenIn = available?.token ?? routeIn
  const tokenOut = held(routeOut)?.token ?? routeOut
  // An unread balance is not an empty one: only a known shortfall blocks.
  const insufficient =
    available !== null && parsed !== null
      ? compareAmounts(parsed, available.amount, tokenIn.decimals) > 0
      : false

  // The engine refuses to price what the wallet cannot fund, so a shortfall
  // is said on the button rather than asked of the bridge.
  const ready = Boolean(wallet) && isPositiveAmount(parsed) && unlocked && !insufficient
  const params: BridgeParams | null = ready
    ? { chainId, toChainId, token: tokenIn.address, amount: parsed as string, wallet }
    : null
  const quote = useBridgeQuote(params)
  // A price kept from the previous params is about another transfer.
  const fresh: BridgeQuote | null = quote.data && !quote.isPlaceholderData ? quote.data : null
  const countdown = quoteCountdown(quote.fetchedAt, now)
  const quoteError = quote.error
    ? quote.error instanceof Error
      ? quote.error.message
      : String(quote.error)
    : null

  // Paying in the gas coin: Max and 100% keep the deposit's own fee back.
  const quoteGasEth =
    quote.data?.fees?.gasUsd != null && available?.priceUsd
      ? String(quote.data.fees.gasUsd / available.priceUsd)
      : null
  const reserve = tokenIn.native ? gasReserveEth(chainId, quoteGasEth) : null
  const spendable = available ? maxSpendable(available.amount, tokenIn.decimals, reserve) : null

  const ctaKey: MessageKey | null = !unlocked
    ? 'trading.swap.cta.locked'
    : !wallet
      ? 'trading.swap.cta.noWallet'
      : !isPositiveAmount(parsed)
        ? 'trading.swap.cta.noAmount'
        : insufficient
          ? 'trading.swap.cta.insufficient'
          : null
  const canReview = ctaKey === null && fresh !== null && !quote.isError

  function flip() {
    // The other way round: what was to arrive is what is now paid.
    setChainId(toChainId)
    setAmount(fresh ? fresh.expectedOut : '')
  }

  const selectedWalletObj = wallets.find((w) => sameAddress(w.address, wallet)) ?? null
  const live = ready && fresh !== null
  const eta = fresh ? formatEta(fresh.timeEstimateSeconds) : null

  const [inRef, inStyle] = useAmountFit<HTMLInputElement>(amount)
  const outText = live ? formatAmount(fresh.expectedOut) : ready && quote.isFetching ? '…' : '0'
  const [outRef, outStyle] = useAmountFit<HTMLOutputElement>(outText)

  return (
    <aside className="trd-ticket" aria-label={t('trading.bridge.title')} data-mode="bridge">
      <div className="trd-ticket__head">
        {onMode ? (
          <TicketModes mode="bridge" onMode={onMode} />
        ) : (
          <span className="trd-ticket__title">{t('trading.bridge.title')}</span>
        )}
        <div role="radiogroup" aria-label={t('trading.bridge.fromChain')} className="mac-segmented">
          {CHAINS.map((c) => (
            <button
              key={c.id}
              type="button"
              role="radio"
              aria-checked={chainId === c.id}
              className="mac-segment app-no-drag"
              onClick={() => pickChain(c.id)}
            >
              {c.short}
            </button>
          ))}
        </div>
      </div>
      <div className="trd-ticket__body">
        <div className="trd-ticket__wallet">
          <span>{t('trading.swap.wallet')}</span>
          <select
            className="mac-select"
            aria-label={t('trading.swap.wallet')}
            value={wallet}
            onChange={(e) => setWalletChoice((c) => ({ ...c, override: e.target.value || null }))}
          >
            {activeFirst(wallets).map((w) => (
              <option key={w.address} value={w.address}>
                {walletLabel(w)}
                {w.primary ? ' ✓' : ''}
              </option>
            ))}
          </select>
        </div>

        <Leg
          label={t('trading.swap.from')}
          chainId={chainId}
          token={tokenIn}
          tokenSlot={
            // Two assets cross, so the picker is a menu, not a search: the
            // native select sits over a button drawn like the swap's.
            <span className="trd-tokenbtn trd-tokenbtn--select app-no-drag">
              <TokenLogo key={`${chainId}:${tokenIn.address}`} token={tokenIn} size={20} />
              {tokenIn.symbol}
              <ChevronDown className="size-3" strokeWidth={2} aria-hidden />
              <select
                aria-label={t('trading.bridge.asset')}
                data-testid="bridge-asset"
                value={asset}
                onChange={(e) => {
                  setAsset(e.target.value as BridgeAsset)
                  setAmount('')
                }}
              >
                {BRIDGE_ASSETS.map((a) => (
                  <option key={a} value={a}>
                    {bridgeToken(chainId, a).symbol}
                  </option>
                ))}
              </select>
            </span>
          }
          balance={available?.amount ?? null}
          onMax={spendable !== null ? () => setAmount(spendable) : undefined}
          onPct={
            spendable !== null
              ? (pct) => setAmount(amountFromPct(spendable, tokenIn.decimals, pct))
              : undefined
          }
          reserve={reserve}
          usd={
            fresh?.valueUsd ??
            (available?.priceUsd !== null && available?.priceUsd !== undefined && parsed
              ? Number(parsed) * available.priceUsd
              : null)
          }
        >
          <input
            className="trd-amount"
            ref={inRef}
            style={inStyle}
            inputMode="decimal"
            autoComplete="off"
            spellCheck={false}
            placeholder={t('trading.swap.amount.placeholder')}
            aria-label={t('trading.swap.amount')}
            aria-invalid={insufficient || (amount !== '' && parsed === null)}
            data-invalid={insufficient || (amount !== '' && parsed === null) ? 'true' : undefined}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
        </Leg>

        <div className="trd-flip">
          <button type="button" aria-label={t('trading.bridge.flip')} onClick={flip}>
            <ArrowDownUp className="size-3.5" strokeWidth={2} aria-hidden />
          </button>
        </div>

        <Leg
          label={t('trading.swap.to')}
          chainId={toChainId}
          token={tokenOut}
          tokenSlot={
            <span className="trd-tokenbtn trd-tokenbtn--fixed" data-testid="bridge-token-out">
              <TokenLogo key={`${toChainId}:${tokenOut.address}`} token={tokenOut} size={20} />
              {tokenOut.symbol}
            </span>
          }
          balance={null}
          usd={null}
        >
          <output
            className="trd-amount trd-amount--out"
            ref={outRef}
            style={outStyle}
            title={outText}
            data-pending={quote.isFetching && !fresh ? 'true' : undefined}
            aria-label={t('trading.swap.to')}
            data-testid="bridge-out"
          >
            {outText}
          </output>
        </Leg>

        {live ? (
          <div className="trd-facts" data-testid="bridge-facts">
            <div className="trd-fact">
              <span>
                {t('trading.bridge.route')}{' '}
                <span className="text-dim" data-testid="bridge-provider">
                  {t('trading.provider.via')} {providerLabel(fresh.provider ?? 'relay')}
                </span>
              </span>
              <b>
                {chainShort(chainId)} → {chainShort(toChainId)}
              </b>
            </div>
            <div className="trd-fact">
              <span>{t('trading.swap.minOut')}</span>
              <b>
                {formatAmount(fresh.minOut)} {tokenOut.symbol}
              </b>
            </div>
            <div className="trd-fact">
              <span>{t('trading.bridge.fees')}</span>
              <b>{formatUsd(fresh.feesUsd)}</b>
            </div>
            {eta ? (
              <div className="trd-fact">
                <span>{t('trading.bridge.eta')}</span>
                <b>{eta}</b>
              </div>
            ) : null}
          </div>
        ) : null}

        {live && fresh.refundGuaranteed === false ? (
          <div className="trd-warn" data-tone="warn" role="status" data-testid="bridge-refund-warn">
            <TriangleAlert className="size-3.5" strokeWidth={2} aria-hidden />
            <span>{t('trading.bridge.refundNotGuaranteed')}</span>
          </div>
        ) : null}

        {live ? (
          <div
            className="trd-guard"
            data-decision={fresh.guard.decision}
            data-testid="bridge-guard"
          >
            {fresh.guard.decision === 'allow' ? (
              <CircleCheck className="size-3.5" strokeWidth={2} aria-hidden />
            ) : (
              <ShieldAlert className="size-3.5" strokeWidth={2} aria-hidden />
            )}
            <span>{t(`trading.swap.guard.${fresh.guard.decision}`)}</span>
          </div>
        ) : null}

        {ready && quote.isError ? (
          <div className="trd-ticket__error" role="alert">
            {quoteError}
          </div>
        ) : null}

        <button
          type="button"
          className="trd-cta app-no-drag"
          disabled={!canReview}
          data-testid="bridge-review"
          data-fresh={live && !countdown.expired ? 'true' : undefined}
          style={
            { '--fresh': live && !countdown.expired ? countdown.fraction : 0 } as CSSProperties
          }
          onClick={() => setConfirm(true)}
        >
          {ctaKey ? t(ctaKey) : t('trading.bridge.cta')}
        </button>
        {ready ? (
          <div className="trd-ticket__fresh" aria-live="polite">
            {quote.isFetching ? (
              t('trading.swap.quoting')
            ) : countdown.expired ? (
              <>
                {t('trading.swap.quoteStale')}
                <button type="button" onClick={() => void quote.refetch()}>
                  {t('trading.swap.requote')}
                </button>
              </>
            ) : (
              <>
                {t('trading.swap.quoteFresh')} {countdown.seconds}s
              </>
            )}
          </div>
        ) : null}
      </div>

      {confirm && fresh && selectedWalletObj ? (
        <ConfirmBridge
          quote={fresh}
          fetchedAt={quote.fetchedAt}
          wallet={selectedWalletObj}
          tokenIn={tokenIn}
          tokenOut={tokenOut}
          amount={parsed as string}
          refreshing={quote.isFetching}
          quoteError={quoteError}
          onRefresh={() => void quote.refetch()}
          onClose={() => setConfirm(false)}
          onSent={(orders) => {
            setConfirm(false)
            setAmount('')
            onSent(orders)
          }}
        />
      ) : null}
    </aside>
  )
}
