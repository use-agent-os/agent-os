import {
  ArrowLeftRight,
  ChevronDown,
  ExternalLink,
  Layers,
  RotateCw,
  Search,
  ShieldAlert,
  TriangleAlert,
} from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import { MenuItem, PopMenu, type MenuPlace } from '~/components/menu/PopMenu'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { shortAge } from '~/lib/relative-time'
import { useNow } from '~/lib/use-now'
import { useMarkets, useTokenSearch } from '~/stores/trading'
import { useTradingUi } from '~/stores/trading-ui'
import { ChainBadge, ChainMark } from './ChainMark'
import { formatPct, formatPrice, pnlTone, shortAddress } from './logic'
import {
  ADDRESS_RE,
  dexLabel,
  feeLabel,
  formatPoolPrice,
  formatRatio,
  counterpartySymbol,
  formatPremiumPct,
  formatUsdShort,
  isNativeCounterparty,
  isStockToken,
  launcherRepeatsDex,
  MIN_TVL_STEPS,
  offersDeeper,
  offersReadAgain,
  pickMarketsTarget,
  poolAge,
  poolSwapTokens,
  premiumTone,
  ROBINHOOD_CHAIN,
  rowPair,
  SECTION_CAP,
  showsCounterpartyName,
  tvlStepLabel,
  type MarketsTarget,
} from './markets-logic'
import { ErrorState, Skeleton, Spinner, Sym, TokenLogo } from './parts'
import { QuoteWarnings } from './SwapPanel'
import {
  CHAINS,
  type ChainId,
  type MarketsPayload,
  type MarketsPool,
  type MarketsSide,
  type MarketsToken,
  type SearchToken,
  type Token,
} from './types'

function fill(text: string, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce((s, [k, v]) => s.split(`{${k}}`).join(String(v)), text)
}

/**
 * Markets: every pool one token trades in, on every DEX of the chain
 * (docs/markets.md). Two lists — the tokens priced in it, then what it is
 * priced in — each row with its venue, depth, flow, price and age, and a
 * Swap that fills the ticket. The search and filters live in the trading-ui
 * store, so a trip to the ticket and back finds the list where it was.
 */
export function Markets({ deskChain = null }: { deskChain?: ChainId | null }) {
  const view = useTradingUi((s) => s.markets)
  const setView = useTradingUi((s) => s.setMarkets)
  const requestSwap = useTradingUi((s) => s.requestSwap)
  const now = useNow(60_000)

  // The field edits a draft; Return commits it. A Holdings row's Markets
  // action commits from outside, and the draft follows.
  const [draft, setDraft] = useState(view.query)
  const [shownQuery, setShownQuery] = useState(view.query)
  if (shownQuery !== view.query) {
    setShownQuery(view.query)
    setDraft(view.query)
  }

  const committed = view.query.trim()
  const explicit = view.chainId !== null
  const chain = view.chainId ?? deskChain ?? ROBINHOOD_CHAIN
  // A symbol is always looked up, even once a token is pinned by address (a
  // Holdings row, or a match picked from the list): the list of the symbol's
  // other matches stays on offer.
  const isSymbol = committed.length > 0 && !ADDRESS_RE.test(committed)
  const search = useTokenSearch(chain, isSymbol ? committed : '')
  // Settled once the lookup has answered; a background refresh of that answer
  // (stale, refocused) keeps the board up instead of dropping to "Looking up…".
  const settled =
    !isSymbol || committed.length < 2 || (search.debounced === committed && !search.isLoading)
  const picked =
    isSymbol && settled ? pickMarketsTarget(committed, search.tokens, chain, explicit) : null
  const resolved: MarketsTarget | null = !committed
    ? null
    : view.address
      ? {
          chainId: chain,
          target: view.address,
          token:
            picked?.candidates.find(
              (c) => c.chainId === chain && c.address.toLowerCase() === view.address!.toLowerCase(),
            ) ?? null,
          candidates: picked?.candidates ?? [],
        }
      : settled
        ? (picked ?? pickMarketsTarget(committed, [], chain, explicit))
        : null

  const markets = useMarkets(
    {
      target: resolved?.target ?? '',
      chainId: resolved?.chainId ?? chain,
      minTvlUsd: view.minTvlUsd,
      lookalikes: view.lookalikes,
      deep: view.deep,
    },
    resolved !== null,
  )
  const data = markets.data
  const shownChain = resolved?.chainId ?? chain

  function submit() {
    const query = draft.trim()
    if (query === view.query && !view.address) {
      if (resolved) void markets.refetch()
      return
    }
    // A new token is a new read: Deeper is per read, offered again if needed.
    setView({ query, address: null, deep: false })
  }

  return (
    <div className="trd-mk" data-testid="markets">
      <form
        className="trd-mk__bar"
        role="search"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <label className="trd-picker__search trd-mk__search">
          <Search className="size-3.5 text-dim" strokeWidth={2} aria-hidden />
          <input
            value={draft}
            placeholder={t('trading.markets.search')}
            spellCheck={false}
            autoComplete="off"
            aria-label={t('trading.markets.search.label')}
            data-testid="markets-search"
            onChange={(e) => setDraft(e.target.value)}
          />
          {(isSymbol && !settled) || markets.isFetching ? <Spinner /> : null}
        </label>
        <div
          role="radiogroup"
          aria-label={t('trading.markets.chain')}
          className="mac-segmented trd-mk__chains"
        >
          {[...CHAINS]
            .sort((a, b) => Number(b.id === ROBINHOOD_CHAIN) - Number(a.id === ROBINHOOD_CHAIN))
            .map((c) => (
              <button
                key={c.id}
                type="button"
                role="radio"
                aria-checked={shownChain === c.id}
                aria-label={c.name}
                title={c.name}
                className="mac-segment app-no-drag"
                data-testid={`markets-chain-${c.id}`}
                onClick={() => setView({ chainId: c.id, address: null, deep: false })}
              >
                <ChainMark chainId={c.id} />
                <span className="trd-mk__chainword">{c.short}</span>
              </button>
            ))}
        </div>
      </form>

      <div className="trd-mk__filters">
        <span className="trd-mk__label">{t('trading.markets.minTvl')}</span>
        <div
          role="radiogroup"
          aria-label={t('trading.markets.minTvl')}
          className="mac-segmented trd-mk__tvl-steps"
        >
          {MIN_TVL_STEPS.map((usd) => (
            <button
              key={usd}
              type="button"
              role="radio"
              aria-checked={view.minTvlUsd === usd}
              className="mac-segment app-no-drag"
              data-testid={`markets-tvl-${usd}`}
              onClick={() => setView({ minTvlUsd: usd })}
            >
              {tvlStepLabel(usd)}
            </button>
          ))}
        </div>
        <label className="trd-mk__toggle" title={t('trading.markets.lookalikes.help')}>
          <Switch
            checked={view.lookalikes}
            onCheckedChange={(on) => setView({ lookalikes: on })}
            aria-label={t('trading.markets.lookalikes')}
            data-testid="markets-lookalikes"
          />
          <span>{t('trading.markets.lookalikes')}</span>
        </label>
        <Button
          variant="ghost"
          size="icon"
          className="trd-mk__refresh"
          aria-label={t('trading.markets.refresh')}
          title={t('trading.markets.refresh')}
          disabled={!resolved || markets.isFetching}
          data-testid="markets-refresh"
          onClick={() => void markets.refetch()}
        >
          <RotateCw
            className="size-3.5 text-muted-foreground"
            strokeWidth={1.75}
            data-spinning={markets.isFetching || undefined}
            aria-hidden
          />
        </Button>
      </div>

      {resolved && isSymbol && (resolved.token || data) ? (
        <Picked
          token={resolved.token}
          fallback={data?.token ?? null}
          fallbackChain={data?.chain.id ?? shownChain}
          candidates={resolved.candidates}
          onPick={(c) => setView({ address: c.address, chainId: c.chainId, deep: false })}
        />
      ) : null}

      {!committed ? (
        <div className="trd-empty" data-testid="markets-prompt">
          <Layers className="size-8" strokeWidth={1.25} aria-hidden />
          <b>{t('trading.markets.prompt.title')}</b>
          <p>{t('trading.markets.prompt.body')}</p>
        </div>
      ) : !resolved ? (
        <div className="trd-mk__wait" data-testid="markets-resolving" aria-busy="true">
          <Spinner />
          <span>{fill(t('trading.markets.resolving'), { query: committed })}</span>
        </div>
      ) : markets.isError && !data ? (
        <div data-testid="markets-error">
          <ErrorState error={markets.error} onRetry={() => void markets.refetch()} />
        </div>
      ) : !data ? (
        <MarketsSkeleton />
      ) : (
        <Board
          data={data}
          now={now}
          minTvlUsd={view.minTvlUsd}
          refreshing={markets.isFetching}
          deep={view.deep}
          onDeeper={() => setView({ deep: true })}
          onReadAgain={() => void markets.refetch()}
          onSwap={(pool) => requestSwap(poolSwapTokens(data, pool))}
        />
      )}
    </div>
  )
}

/**
 * Which token the typed symbol was taken to mean — "Showing Artificial Inu ·
 * 0x2e8c…1e18" — and, when the symbol is several verified tokens, the list of
 * them to choose another from. A symbol is never resolved silently.
 */
function Picked({
  token,
  fallback,
  fallbackChain,
  candidates,
  onPick,
}: {
  token: SearchToken | null
  fallback: MarketsToken | null
  fallbackChain: number
  candidates: SearchToken[]
  onPick: (match: SearchToken) => void
}) {
  // The anchor rect is captured on open; the list is portalled out of the
  // tab's scroller and sized for a token's full name (not the house 200 px).
  const [place, setPlace] = useState<MenuPlace | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const close = useCallback(() => setPlace(null), [])
  const open = place !== null
  const shown = token ?? fallback
  if (!shown) return null
  const chainId = token?.chainId ?? fallbackChain
  const name = shown.name || shown.symbol
  const isShown = (c: SearchToken) =>
    c.chainId === chainId && c.address.toLowerCase() === shown.address.toLowerCase()
  return (
    <div className="trd-mk__picked" data-testid="markets-picked">
      <div className="trd-mk__showing">
        <span className="trd-mk__showname">{fill(t('trading.markets.showing'), { name })}</span>
        <span aria-hidden> · </span>
        <span className="trd-num trd-mk__showaddr" title={shown.address}>
          {shortAddress(shown.address)}
        </span>
        {candidates.length > 1 ? (
          <span className="trd-mk__matchwrap">
            <button
              ref={triggerRef}
              type="button"
              className="trd-mk__matches app-no-drag"
              aria-expanded={open}
              aria-haspopup="menu"
              data-testid="markets-matches"
              onClick={(e) => {
                const rect = e.currentTarget.getBoundingClientRect()
                setPlace((p) => (p ? null : { anchor: rect, align: 'end' }))
              }}
            >
              {fill(t('trading.markets.matches'), { count: candidates.length })}
              <ChevronDown className="size-3" strokeWidth={2} aria-hidden />
            </button>
            {place ? (
              <PopMenu
                place={place}
                triggerRef={triggerRef}
                onClose={close}
                label={t('trading.markets.matches.label')}
                className="trd-mk__matchmenu"
              >
                {candidates.map((c) => (
                  <MenuItem
                    key={`${c.chainId}:${c.address}`}
                    mark={<TokenLogo token={c} size={14} />}
                    label={c.name || c.symbol}
                    detail={
                      <span className="trd-mk__matchdetail" title={c.address}>
                        <span className="trd-sym">{c.symbol}</span>
                        <span aria-hidden> · </span>
                        <span className="trd-num">{shortAddress(c.address)}</span>
                        {isStockToken(c) ? (
                          <>
                            <span aria-hidden> · </span>
                            <span>{t('trading.markets.stock')}</span>
                          </>
                        ) : null}
                      </span>
                    }
                    aside={
                      <span className="trd-mk__matchliq" data-testid="markets-match-liquidity">
                        <small>{t('trading.markets.liquidity')}</small>
                        <b className="trd-num">{formatUsdShort(c.liquidityUsd)}</b>
                      </span>
                    }
                    checked={isShown(c)}
                    onSelect={() => {
                      if (!isShown(c)) onPick(c)
                    }}
                  />
                ))}
              </PopMenu>
            ) : null}
          </span>
        ) : null}
      </div>
    </div>
  )
}

function MarketsSkeleton() {
  return (
    <div className="trd-mk__skeleton" data-testid="markets-loading" aria-busy="true">
      <p className="trd-mk__slow" role="status">
        <Spinner />
        <span>
          {t('trading.markets.loading')} {t('trading.markets.loading.slow')}
        </span>
      </p>
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="trd-mk__skelrow">
          <Skeleton width={i % 2 ? 96 : 120} />
          <Skeleton width={56} />
          <Skeleton width={44} />
        </div>
      ))}
    </div>
  )
}

function Board({
  data,
  now,
  minTvlUsd,
  refreshing,
  deep,
  onDeeper,
  onReadAgain,
  onSwap,
}: {
  data: MarketsPayload
  now: number
  minTvlUsd: number
  refreshing: boolean
  deep: boolean
  onDeeper: () => void
  onReadAgain: () => void
  onSwap: (pool: MarketsPool) => void
}) {
  const tvl = tvlStepLabel(data.request?.params?.minTvlUsd ?? minTvlUsd)
  const nothing = data.sections.quote.length === 0 && data.sections.base.length === 0
  const c = data.counts
  const wasDeep = deep || data.request?.params?.deep === true
  return (
    <div className="trd-mk__board" data-refreshing={refreshing || undefined}>
      <TokenHead data={data} now={now} />
      {nothing ? (
        <div className="trd-empty" data-testid="markets-empty">
          <Layers className="size-8" strokeWidth={1.25} aria-hidden />
          <b>{fill(t('trading.markets.empty.title'), { tvl })}</b>
          <p>{t('trading.markets.empty.body')}</p>
        </div>
      ) : (
        (['quote', 'base'] as const).map((side) => (
          <Section
            key={side}
            side={side}
            rows={data.sections[side]}
            data={data}
            tvl={tvl}
            now={now}
            onSwap={onSwap}
          />
        ))
      )}
      <footer className="trd-mk__foot" data-testid="markets-foot">
        <span className="trd-mk__counts trd-num">
          {[
            fill(t('trading.markets.counts.shown'), { shown: c.shown, scanned: c.scanned }),
            c.belowMinTvl > 0
              ? fill(t('trading.markets.counts.below'), { count: c.belowMinTvl, tvl })
              : null,
            c.hiddenLookalikes > 0
              ? fill(t('trading.markets.counts.lookalikes'), { count: c.hiddenLookalikes })
              : null,
            (c.limited ?? 0) > 0
              ? fill(t('trading.markets.counts.limited'), { count: c.limited ?? 0 })
              : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        </span>
        {data.partial ? (
          <span
            className="mac-chip trd-mk__partial"
            data-tone="warn"
            title={t('trading.markets.partial.help')}
            data-testid="markets-partial"
          >
            <TriangleAlert className="size-2.5" strokeWidth={2.25} aria-hidden />
            {t('trading.markets.partial')}
          </span>
        ) : null}
        {offersDeeper(c, wasDeep) ? (
          <button
            type="button"
            className="trd-chip trd-mk__deeper app-no-drag"
            title={t('trading.markets.deep.help')}
            disabled={refreshing}
            data-testid="markets-deep"
            onClick={onDeeper}
          >
            <Layers className="size-3" strokeWidth={2} aria-hidden />
            <span className="trd-chip__label">{t('trading.markets.deep')}</span>
          </button>
        ) : null}
        {offersReadAgain(c, wasDeep) ? (
          <button
            type="button"
            className="trd-chip trd-mk__deeper app-no-drag"
            title={t('trading.markets.again.help')}
            disabled={refreshing}
            data-testid="markets-again"
            onClick={onReadAgain}
          >
            <RotateCw className="size-3" strokeWidth={2} aria-hidden />
            <span className="trd-chip__label">{t('trading.markets.refresh')}</span>
          </button>
        ) : null}
        {c.rateLimited ? (
          <p className="trd-mk__ratelimited" role="status" data-testid="markets-ratelimited">
            <TriangleAlert className="size-3" strokeWidth={2} aria-hidden />
            <span>{t('trading.markets.rateLimited')}</span>
          </p>
        ) : null}
      </footer>
      <QuoteWarnings warnings={data.warnings} />
    </div>
  )
}

function TokenHead({ data, now }: { data: MarketsPayload; now: number }) {
  const tk = data.token
  const oracle = tk.oracle
  const logo: Token = {
    chainId: data.chain.id,
    address: tk.address,
    symbol: tk.symbol,
    name: tk.name,
    decimals: tk.decimals,
    logoUrl: tk.logoUrl,
    native: false,
    verified: tk.verified,
  }
  const oracleTone = oracle?.paused ? 'danger' : oracle?.stale ? 'warn' : null
  return (
    <header className="trd-mk__head" data-testid="markets-head">
      <TokenLogo token={logo} size={30} />
      <div className="trd-mk__id">
        <span className="trd-mk__sym">
          <Sym symbol={tk.symbol} />
          <ChainBadge chainId={data.chain.id} className="trd-asset__chain" />
          {tk.stockToken ? (
            <span className="mac-chip" data-tone="ok">
              {t('trading.markets.stock')}
            </span>
          ) : null}
        </span>
        <span className="trd-mk__name" title={tk.name}>
          {tk.name}
        </span>
      </div>
      <div className="trd-mk__quote">
        <b className="trd-num" title={t('trading.markets.price')}>
          {formatPrice(tk.priceUsd)}
        </b>
        {oracle ? (
          <span
            className="trd-mk__oracle"
            title={fill(t('trading.markets.oracle.help'), {
              age: shortAge(now - oracle.ageSeconds * 1000, now),
            })}
            data-testid="markets-oracle"
          >
            <span>{t('trading.markets.oracle')}</span>
            <span className="trd-num">{formatPrice(oracle.usd)}</span>
            {oracleTone ? (
              <span className="mac-chip" data-tone={oracleTone} data-testid="markets-oracle-badge">
                {oracle.paused
                  ? t('trading.markets.oracle.paused')
                  : t('trading.markets.oracle.stale')}
              </span>
            ) : null}
          </span>
        ) : null}
      </div>
    </header>
  )
}

function Section({
  side,
  rows,
  data,
  tvl,
  now,
  onSwap,
}: {
  side: MarketsSide
  rows: MarketsPool[]
  data: MarketsPayload
  tvl: string
  now: number
  onSwap: (pool: MarketsPool) => void
}) {
  const [open, setOpen] = useState(false)
  const symbol = data.token.symbol
  const shown = open ? rows : rows.slice(0, SECTION_CAP)
  const more = rows.length - shown.length
  return (
    <section className="trd-mk__section" data-side={side} data-testid={`markets-section-${side}`}>
      <h3
        className="trd-mk__title"
        title={fill(t(`trading.markets.section.${side}.help`), { symbol })}
      >
        <span>{fill(t(`trading.markets.section.${side}`), { symbol })}</span>
        <span className="trd-mk__count trd-num">{rows.length}</span>
      </h3>
      {rows.length === 0 ? (
        <p className="trd-mk__none">{fill(t('trading.markets.section.empty'), { symbol, tvl })}</p>
      ) : (
        <>
          <div className="trd-mk__cols" aria-hidden>
            <span>{t('trading.markets.col.pair')}</span>
            <span>{t('trading.markets.col.venue')}</span>
            <span>{t('trading.markets.col.tvl')}</span>
            <span>{t('trading.markets.col.volume')}</span>
            <span>{t('trading.markets.col.price')}</span>
            <span>{t('trading.markets.col.change')}</span>
            <span>{t('trading.markets.col.age')}</span>
            <ActSizer />
          </div>
          <ul className="trd-mk__rows">
            {shown.map((pool) => (
              <Row
                key={`${pool.side}:${pool.poolAddress}`}
                pool={pool}
                token={data.token}
                now={now}
                onSwap={onSwap}
              />
            ))}
          </ul>
          {more > 0 ? (
            <button
              type="button"
              className="trd-mk__more app-no-drag"
              data-testid={`markets-more-${side}`}
              onClick={() => setOpen(true)}
            >
              {fill(t('trading.markets.more'), { count: more })}
            </button>
          ) : null}
        </>
      )}
    </section>
  )
}

/**
 * The head's last cell: an invisible copy of a row's buttons. The head and each
 * row are separate grids sharing one column template, so an empty `auto` act
 * column here would be 0px while every row's is as wide as its buttons — and
 * the head's flexible columns would run wider than the values under them.
 */
function ActSizer() {
  return (
    <span className="trd-mk__act trd-mk__act--sizer">
      <span className="mac-button trd-mk__open" data-variant="ghost" data-size="icon">
        <ExternalLink className="size-3" strokeWidth={1.75} />
      </span>
      <span className="mac-button trd-mk__swap" data-variant="secondary" data-size="md">
        <ArrowLeftRight className="size-3" strokeWidth={2} />
        <span>{t('trading.markets.swap')}</span>
      </span>
    </span>
  )
}

function Row({
  pool,
  token,
  now,
  onSwap,
}: {
  pool: MarketsPool
  token: MarketsToken
  now: number
  onSwap: (pool: MarketsPool) => void
}) {
  const [first, second] = rowPair(pool, token.symbol)
  const cp = pool.counterparty
  const native = isNativeCounterparty(cp)
  const fee = feeLabel(pool.feePct)
  const logo: Token = {
    chainId: pool.swap.chainId,
    address: cp.address,
    symbol: native ? 'ETH' : cp.symbol,
    name: cp.name,
    decimals: cp.decimals,
    logoUrl: cp.logoUrl,
    native,
    verified: native || cp.verified,
  }
  const symbol = token.symbol
  const named = showsCounterpartyName(pool, symbol)
  const legs = poolSwapTokens({ token }, pool)
  const swapLabel = fill(t('trading.markets.swap.label'), {
    from: legs.tokenIn.symbol,
    to: legs.tokenOut.symbol,
  })
  // A launchpad's own DEX (Bankr on Bankr) needs no second word for it: the
  // venue itself wears the launcher's tint.
  const launchedHere = launcherRepeatsDex(pool)
  const launchedTitle = pool.launcher
    ? fill(t('trading.markets.launcher'), { name: pool.launcher })
    : undefined
  return (
    <li
      className="trd-mk__row"
      data-side={pool.side}
      data-lookalike={cp.lookalike || undefined}
      data-testid="markets-row"
    >
      <span className="trd-mk__pair" title={cp.name}>
        <TokenLogo token={logo} size={18} />
        <span className="trd-mk__pairtext" title={`${first}/${second}`}>
          <Sym symbol={first} className="trd-mk__first" />
          <span className="trd-mk__slash">/</span>
          <Sym symbol={second} className="trd-mk__second" />
        </span>
        {named ? (
          <span className="trd-mk__cpname" data-testid="markets-cpname" title={cp.address}>
            {cp.name}
          </span>
        ) : null}
        {cp.lookalike ? (
          <span
            className="mac-chip trd-mk__flag"
            data-tone="warn"
            data-kind="lookalike"
            title={t('trading.markets.flag.lookalike.help')}
          >
            <ShieldAlert className="size-2.5" strokeWidth={2.25} aria-hidden />
            {t('trading.markets.flag.lookalike')}
          </span>
        ) : null}
      </span>
      <span className="trd-mk__venue">
        <span
          className="trd-mk__dex"
          data-launcher={launchedHere || undefined}
          title={launchedHere ? launchedTitle : undefined}
        >
          {dexLabel({ ...pool.dex, version: null })}
        </span>
        {pool.dex.version ? <span className="trd-mk__ver">{pool.dex.version}</span> : null}
        {fee ? (
          <span className="trd-mk__fee trd-num" title={fill(t('trading.markets.fee'), { fee })}>
            {fee}
          </span>
        ) : null}
        {pool.launcher && !launchedHere ? (
          <span className="mac-chip trd-mk__launcher" title={launchedTitle}>
            {pool.launcher}
          </span>
        ) : null}
        {pool.viaUniswap ? (
          <span
            className="mac-chip trd-mk__flag"
            data-tone="primary"
            data-kind="uni"
            title={t('trading.markets.flag.uni.help')}
          >
            {t('trading.markets.flag.uni')}
          </span>
        ) : null}
        {cp.stockToken ? (
          <span
            className="mac-chip trd-mk__flag"
            data-tone="ok"
            data-kind="stock"
            title={t('trading.markets.flag.stock.help')}
          >
            {t('trading.markets.flag.stock')}
          </span>
        ) : null}
      </span>
      <span className="trd-mk__tvl trd-num" title={t('trading.markets.tvl')}>
        {formatUsdShort(pool.tvlUsd)}
      </span>
      <span
        className="trd-mk__vol trd-num"
        title={
          pool.txns24h
            ? fill(t('trading.markets.txns'), {
                buys: pool.txns24h.buys,
                sells: pool.txns24h.sells,
              })
            : t('trading.markets.volume')
        }
      >
        {formatUsdShort(pool.volume24hUsd)}
      </span>
      {/* USD, then the price in the other token, then (base rows) the premium:
          each on its own line, so none runs into the next or into Age. */}
      <span className="trd-mk__px trd-num">
        <span className="trd-mk__usd">{formatPoolPrice(pool.priceUsd)}</span>
        <small className="trd-mk__in">
          {formatRatio(pool.priceInToken)}{' '}
          {pool.side === 'quote' ? symbol : second || counterpartySymbol(cp)}
        </small>
        {pool.premiumPct !== null && pool.side === 'base' ? (
          <small
            className="trd-mk__premium"
            data-tone={premiumTone(pool.premiumPct)}
            data-testid="markets-premium"
          >
            {fill(t('trading.markets.premium'), { pct: formatPremiumPct(pool.premiumPct) })}
          </small>
        ) : null}
      </span>
      <span
        className="trd-mk__chg trd-num"
        data-tone={pnlTone(pool.change24hPct)}
        title={t('trading.markets.change')}
      >
        {formatPct(pool.change24hPct, { signed: true })}
      </span>
      <span className="trd-mk__age trd-num" title={t('trading.markets.age')}>
        {poolAge(pool.createdAt, now)}
      </span>
      <span className="trd-mk__act">
        <Button
          variant="ghost"
          size="icon"
          aria-label={t('trading.markets.open')}
          title={t('trading.markets.open')}
          className="trd-mk__open"
          onClick={() => void desktopApi().app.openExternal(pool.url)}
        >
          <ExternalLink className="size-3 text-muted-foreground" strokeWidth={1.75} aria-hidden />
        </Button>
        <Button
          size="md"
          className="trd-mk__swap"
          aria-label={swapLabel}
          title={t('trading.markets.swap.help')}
          data-testid="markets-swap"
          onClick={() => onSwap(pool)}
        >
          <ArrowLeftRight className="size-3" strokeWidth={2} aria-hidden />
          <span>{t('trading.markets.swap')}</span>
        </Button>
      </span>
    </li>
  )
}
