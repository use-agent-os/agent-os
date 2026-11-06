import {
  ArrowDownUp,
  ChevronDown,
  CircleCheck,
  ShieldAlert,
  TriangleAlert,
  Zap,
} from 'lucide-react'
import { useMemo, useState, type CSSProperties } from 'react'
import { t, type MessageKey } from '~/i18n'
import { useNow } from '~/lib/use-now'
import { useBalances, useQuote, type QuoteParams } from '~/stores/trading'
import { ConfirmSwap } from './ConfirmSwap'
import {
  activeFirst,
  amountFromPct,
  compareAmounts,
  formatAmount,
  formatPct,
  formatUsd,
  gasReserveEth,
  impactTone,
  isPositiveAmount,
  maxSpendable,
  parseAmount,
  quoteCountdown,
  sameAddress,
  sameToken,
  walletLabel,
} from './logic'
import { TokenLogo } from './parts'
import { nativeToken, TokenPicker } from './TokenPicker'
import {
  CHAINS,
  providerLabel,
  type Balance,
  type ChainId,
  type Order,
  type ProviderId,
  type Quote,
  type Token,
  type Wallet,
} from './types'

/** What the provider flagged about this pair (a transfer fee, say): shown, never hidden. */
export function QuoteWarnings({ warnings }: { warnings: string[] | undefined }) {
  const rows = (warnings ?? []).map((w) => String(w).trim()).filter(Boolean)
  if (rows.length === 0) return null
  return (
    <ul className="trd-warnings" role="status" data-testid="quote-warnings">
      {rows.map((w) => (
        <li key={w}>
          <TriangleAlert className="size-3" strokeWidth={2} aria-hidden />
          <span>{w}</span>
        </li>
      ))}
    </ul>
  )
}

export interface SwapPrefill {
  chainId: number
  tokenIn?: Token
  tokenOut?: Token
  wallet?: string
  /** Bumps so the same prefill can be applied twice. */
  seq: number
}

type Slippage = 'auto' | string

/**
 * The ticket. One wallet, one chain, two legs, a live price with its age
 * on the button. It never sends anything itself: Review opens the confirm
 * sheet, which is the only place a swap leaves from.
 */
export function SwapPanel({
  wallets,
  primary,
  selectedWallet,
  provider,
  providerReady,
  onOpenSettings,
  unlocked,
  prefill,
  onSent,
}: {
  wallets: Wallet[]
  primary: string | null
  /** The rail's selection: an address, or 'all'. */
  selectedWallet: string
  provider: ProviderId
  /** Uniswap has its key, or the keyless aggregator is selected. */
  providerReady: boolean
  /** Opens Settings › Trading, the one place a Uniswap key is added. */
  onOpenSettings: () => void
  unlocked: boolean
  prefill: SwapPrefill | null
  onSent: (orders: Order[]) => void
}) {
  const [chainId, setChainId] = useState<ChainId>(8453)
  const [tokenIn, setTokenIn] = useState<Token | null>(nativeToken(8453))
  const [tokenOut, setTokenOut] = useState<Token | null>(null)
  const [amount, setAmount] = useState('')
  const [slippage, setSlippage] = useState<Slippage>('auto')
  const [picker, setPicker] = useState<'in' | 'out' | null>(null)
  const [confirm, setConfirm] = useState(false)
  const now = useNow(1000)

  // The rail's choice follows into the ticket ("all" means the primary)
  // until the ticket's own menu overrides it; a new rail choice resets that.
  const [walletChoice, setWalletChoice] = useState<{ base: string; override: string | null }>({
    base: selectedWallet,
    override: null,
  })
  if (walletChoice.base !== selectedWallet) {
    setWalletChoice({ base: selectedWallet, override: null })
  }
  const followed =
    selectedWallet !== 'all' ? selectedWallet : (primary ?? wallets[0]?.address ?? '')
  // An override must still be a wallet that exists: a removed one falls back.
  const override =
    walletChoice.override && wallets.some((w) => sameAddress(w.address, walletChoice.override))
      ? walletChoice.override
      : null
  const wallet = override ?? followed
  const setWallet = (address: string) =>
    setWalletChoice((c) => ({ ...c, override: address || null }))

  // A row's Swap button fills the ticket: applied once per prefill.
  const [appliedSeq, setAppliedSeq] = useState<number | null>(null)
  if (prefill && prefill.seq !== appliedSeq) {
    setAppliedSeq(prefill.seq)
    if (prefill.chainId === 8453 || prefill.chainId === 4663) setChainId(prefill.chainId)
    if (prefill.tokenIn) setTokenIn(prefill.tokenIn)
    else if (prefill.tokenOut) setTokenIn(nativeToken(prefill.chainId))
    if (prefill.tokenOut !== undefined) setTokenOut(prefill.tokenOut ?? null)
    if (prefill.wallet) setWalletChoice((c) => ({ ...c, override: prefill.wallet ?? null }))
    setAmount('')
  }

  function pickChain(next: ChainId) {
    if (next === chainId) return
    setChainId(next)
    setTokenIn(nativeToken(next))
    setTokenOut(null)
    setAmount('')
  }

  const parsed = parseAmount(amount)
  // The ticket reads the paying wallet's own balances, never the rail's mix.
  const { balances } = useBalances(wallet || undefined, Boolean(wallet))
  const available: Balance | null = useMemo(
    () => balances.find((b) => b.chainId === chainId && sameToken(b.token, tokenIn)) ?? null,
    [balances, chainId, tokenIn],
  )
  const insufficient =
    available !== null && tokenIn !== null && parsed !== null
      ? compareAmounts(parsed, available.amount, tokenIn.decimals) > 0
      : false
  const slippagePct = slippage === 'auto' ? undefined : Number(slippage)
  const slippageValid =
    slippage === 'auto' ||
    (Number.isFinite(slippagePct) && (slippagePct as number) > 0 && (slippagePct as number) <= 50)

  const ready =
    Boolean(wallet) &&
    tokenIn !== null &&
    tokenOut !== null &&
    !sameToken(tokenIn, tokenOut) &&
    isPositiveAmount(parsed) &&
    providerReady &&
    unlocked &&
    slippageValid

  const params: QuoteParams | null = ready
    ? {
        chainId,
        wallet,
        tokenIn: (tokenIn as Token).address,
        tokenOut: (tokenOut as Token).address,
        amountIn: parsed as string,
        ...(slippagePct !== undefined ? { slippagePct } : {}),
      }
    : null
  const quote = useQuote(params)
  // The query keeps the previous params' price as a placeholder while the new
  // one loads. That price is about another swap: nothing on the ticket may
  // show it, and Review must not open on it.
  const fresh: Quote | null = quote.data && !quote.isPlaceholderData ? quote.data : null
  // The engine says when it stops honouring the price; the 15 s refetch
  // cadence is unchanged, this only decides when the ring reads "expired".
  const countdown = quoteCountdown(quote.fetchedAt, now, fresh?.expiresAt)
  const quoteError = quote.error
    ? quote.error instanceof Error
      ? quote.error.message
      : String(quote.error)
    : null

  // Paying in the gas coin: Max and 100% keep enough back for the swap's own
  // fee. The latest quote's fee (in ETH, via its price) sets the floor when
  // it is higher than the chain's default.
  const quoteGasEth =
    quote.data?.gasUsd != null && available?.priceUsd
      ? String(quote.data.gasUsd / available.priceUsd)
      : null
  const reserve = tokenIn?.native ? gasReserveEth(chainId, quoteGasEth) : null
  const spendable =
    available && tokenIn ? maxSpendable(available.amount, tokenIn.decimals, reserve) : null

  const ctaKey: MessageKey | null = !unlocked
    ? 'trading.swap.cta.locked'
    : !providerReady
      ? provider === 'uniswap'
        ? 'trading.swap.cta.noKey'
        : 'trading.provider.switch'
      : !wallet
        ? 'trading.swap.cta.noWallet'
        : !tokenIn || !tokenOut
          ? 'trading.swap.pick'
          : sameToken(tokenIn, tokenOut)
            ? 'trading.swap.cta.sameToken'
            : !isPositiveAmount(parsed)
              ? 'trading.swap.cta.noAmount'
              : insufficient
                ? 'trading.swap.cta.insufficient'
                : null
  const canReview = ctaKey === null && fresh !== null && !quote.isError
  // A gate the ticket can open itself: the button does that instead of sitting
  // dead. Only Uniswap can be un-ready, and only for want of its key.
  const gateAction: (() => void) | null = unlocked && !providerReady ? onOpenSettings : null

  function flip() {
    setTokenIn(tokenOut)
    setTokenOut(tokenIn)
    setAmount(fresh && tokenOut ? fresh.amountOut : '')
  }

  const selectedWalletObj = wallets.find((w) => sameAddress(w.address, wallet)) ?? null

  return (
    <aside className="trd-ticket" aria-label={t('trading.swap.title')}>
      <div className="trd-ticket__head">
        <span className="trd-ticket__title">{t('trading.swap.title')}</span>
        <div role="radiogroup" aria-label={t('trading.overview.chains')} className="mac-segmented">
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
            onChange={(e) => setWallet(e.target.value)}
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
          token={tokenIn}
          onPick={() => setPicker('in')}
          balance={available?.amount ?? null}
          onMax={spendable !== null ? () => setAmount(spendable) : undefined}
          onPct={
            spendable !== null && tokenIn
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
          <button
            type="button"
            aria-label={t('trading.swap.flip')}
            onClick={flip}
            disabled={!tokenOut}
          >
            <ArrowDownUp className="size-3.5" strokeWidth={2} aria-hidden />
          </button>
        </div>

        <Leg
          label={t('trading.swap.to')}
          token={tokenOut}
          onPick={() => setPicker('out')}
          balance={null}
          usd={null}
        >
          <output
            className="trd-amount trd-amount--out"
            data-pending={quote.isFetching && !fresh ? 'true' : undefined}
            aria-label={t('trading.swap.to')}
            data-testid="quote-out"
          >
            {fresh && ready ? formatAmount(fresh.amountOut) : quote.isFetching ? '…' : '0'}
          </output>
        </Leg>

        {ready && fresh ? (
          <div className="trd-facts" data-testid="quote-facts">
            <div className="trd-fact">
              <span>
                {t('trading.swap.rate')}{' '}
                <span className="text-dim" data-testid="quote-provider">
                  {t('trading.provider.via')} {providerLabel(fresh.provider ?? provider)}
                </span>
              </span>
              <b>
                1 {tokenIn?.symbol} ≈ {formatAmount(fresh.rate)} {tokenOut?.symbol}
              </b>
            </div>
            <div className="trd-fact">
              <span>{t('trading.swap.impact')}</span>
              <b data-tone={impactTone(fresh.priceImpactPct)}>{formatPct(fresh.priceImpactPct)}</b>
            </div>
            <div className="trd-fact">
              <span>{t('trading.swap.minOut')}</span>
              <b>
                {formatAmount(fresh.minOut)} {tokenOut?.symbol}
              </b>
            </div>
            <div className="trd-fact">
              <span>{t('trading.swap.gas')}</span>
              <b>{formatUsd(fresh.gasUsd)}</b>
            </div>
            <div className="trd-fact">
              <span>{t('trading.swap.slippage')}</span>
              <span className="trd-fact__slip">
                <select
                  className="mac-select trd-fact__select"
                  aria-label={t('trading.swap.slippage')}
                  value={slippage === 'auto' ? 'auto' : 'custom'}
                  onChange={(e) =>
                    setSlippage(
                      e.target.value === 'auto' ? 'auto' : String(fresh?.slippagePct ?? 0.5),
                    )
                  }
                >
                  <option value="auto">{t('trading.swap.slippage.auto')}</option>
                  <option value="custom">{t('trading.swap.slippage.custom')}</option>
                </select>
                {slippage !== 'auto' ? (
                  <input
                    className="mac-input"
                    inputMode="decimal"
                    aria-label={t('trading.swap.slippage')}
                    data-invalid={!slippageValid ? 'true' : undefined}
                    value={slippage}
                    onChange={(e) => setSlippage(e.target.value)}
                  />
                ) : (
                  <b>{formatPct(fresh.slippagePct)}</b>
                )}
              </span>
            </div>
          </div>
        ) : null}

        {ready && fresh ? <QuoteWarnings warnings={fresh.warnings} /> : null}

        {ready && fresh ? (
          <div className="trd-guard" data-decision={fresh.guard.decision} data-testid="quote-guard">
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
            {quoteError?.includes('no_route') || quoteError?.includes('NoRoute')
              ? t('trading.swap.noRoute')
              : quoteError}
          </div>
        ) : null}

        {/* The quote's remaining life is the button's own bottom edge, not a
            ring beside the label: it keeps the label centred, and it is the
            one thing on this panel that is about *this* action. The line
            below still says it in words. */}
        <button
          type="button"
          className="trd-cta app-no-drag"
          disabled={gateAction ? false : !canReview}
          data-testid="swap-review"
          data-fresh={ready && fresh && !countdown.expired ? 'true' : undefined}
          style={
            {
              '--fresh': ready && fresh && !countdown.expired ? countdown.fraction : 0,
            } as CSSProperties
          }
          onClick={gateAction ?? (() => setConfirm(true))}
        >
          {ctaKey ? t(ctaKey) : t('trading.swap.cta')}
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

      {picker ? (
        <TokenPicker
          chainId={chainId}
          balances={balances}
          exclude={picker === 'in' ? tokenOut : tokenIn}
          onPick={(tk) => {
            // The picker searches every chain, so a choice can move the
            // ticket. The other leg belonged to the chain we just left, so it
            // cannot come along: the pay side falls back to that chain's gas
            // coin, the receive side clears.
            if (tk.chainId !== chainId && (tk.chainId === 8453 || tk.chainId === 4663)) {
              setChainId(tk.chainId)
              setAmount('')
              if (picker === 'in') {
                setTokenIn(tk)
                setTokenOut(null)
              } else {
                setTokenIn(nativeToken(tk.chainId))
                setTokenOut(tk)
              }
            } else if (picker === 'in') {
              setTokenIn(tk)
            } else {
              setTokenOut(tk)
            }
            setPicker(null)
          }}
          onClose={() => setPicker(null)}
        />
      ) : null}

      {confirm && fresh && tokenIn && tokenOut && selectedWalletObj ? (
        <ConfirmSwap
          quote={fresh}
          fetchedAt={quote.fetchedAt}
          wallet={selectedWalletObj}
          tokenIn={tokenIn}
          tokenOut={tokenOut}
          amount={parsed as string}
          slippagePct={slippagePct}
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

function Leg({
  label,
  token,
  onPick,
  balance,
  onMax,
  onPct,
  usd,
  reserve,
  children,
}: {
  label: string
  token: Token | null
  onPick: () => void
  balance: string | null
  onMax?: () => void
  onPct?: (pct: number) => void
  usd: number | null
  /** What Max holds back for gas, when the leg pays in the gas coin. */
  reserve?: string | null
  children: React.ReactNode
}) {
  return (
    <div className="trd-leg">
      <div className="trd-leg__row">
        <span className="trd-leg__label">{label}</span>
        {balance !== null ? (
          <span className="trd-leg__balance">
            {t('trading.swap.balance')} {formatAmount(balance)}
            {onMax ? (
              <button type="button" onClick={onMax}>
                {t('trading.swap.max')}
              </button>
            ) : null}
          </span>
        ) : null}
      </div>
      <div className="trd-leg__main">
        <button
          type="button"
          className="trd-tokenbtn app-no-drag"
          data-empty={token ? undefined : 'true'}
          onClick={onPick}
          data-testid="token-button"
        >
          {token ? (
            <>
              <TokenLogo token={token} size={20} />
              {token.symbol}
            </>
          ) : (
            <>
              <Zap className="size-3.5" strokeWidth={2} aria-hidden />
              {t('trading.swap.pick')}
            </>
          )}
          <ChevronDown className="size-3" strokeWidth={2} aria-hidden />
        </button>
        {children}
      </div>
      <div className="trd-leg__row">
        {onPct ? (
          <span className="trd-pcts" aria-label={t('trading.swap.amount')}>
            {[25, 50, 75, 100].map((p) => (
              <button key={p} type="button" onClick={() => onPct(p)}>
                {p}%
              </button>
            ))}
          </span>
        ) : (
          <span />
        )}
        <span className="trd-leg__usd">{usd !== null ? `≈ ${formatUsd(usd)}` : ''}</span>
      </div>
      {reserve && onMax ? (
        <span className="trd-leg__reserve" data-testid="gas-reserve">
          {t('trading.swap.gasReserve')} ~{formatAmount(reserve)} {token?.symbol ?? 'ETH'}{' '}
          {t('trading.swap.gasReserve.for')}
        </span>
      ) : null}
    </div>
  )
}
