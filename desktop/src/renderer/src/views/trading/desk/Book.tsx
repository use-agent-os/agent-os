import {
  ArrowLeftRight,
  BookOpen,
  Bot,
  ChevronsLeft,
  ChevronsRight,
  History as HistoryIcon,
  Layers,
  ListChecks,
  Maximize2,
  TrendingDown,
  TrendingUp,
  Wrench,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined } from '~/lib/biometric-gate'
import { toastOrder, toastOrderRejected, toastOrderSending } from '~/lib/order-toasts'
import { useNow } from '~/lib/use-now'
import { useTradingUi, type BookTab } from '~/stores/trading-ui'
import {
  useHistory,
  useOrderDecision,
  useOrders,
  usePortfolio,
  useTokenVisibility,
  useTradingStatus,
  useWalletMutation,
} from '~/stores/trading'
import { DecodeSheet } from '../DecodeSheet'
import { History } from '../History'
import { Holdings } from '../Holdings'
import { Markets } from '../Markets'
import { NetworkPips } from '../NetworkPips'
import {
  allocationSegments,
  EMPTY_TOTALS,
  errorText,
  formatPct,
  formatUsd,
  isAwaitingApproval,
  pnlTone,
  sameAddress,
} from '../logic'
import { Orders } from '../Orders'
import { Money, PanelBoundary, useCountUp } from '../parts'
import { SwapPanel, type SwapPrefill } from '../SwapPanel'
import type { Holding, Order, ProviderId, Totals, Wallet } from '../types'
import { WalletHead } from '../WalletHead'
import { WalletSheet, type WalletSheetMode } from '../WalletSheet'
import { Automations } from './Automations'
import { BOOK_MAX, BOOK_MIN, watchTabStrip } from './desk-logic'
import type { MissionsApi } from './missions'
import { ToolsPanel } from './ToolsPanel'

const TABS: readonly { id: BookTab; icon: typeof BookOpen }[] = [
  { id: 'portfolio', icon: BookOpen },
  { id: 'swap', icon: ArrowLeftRight },
  { id: 'markets', icon: Layers },
  { id: 'orders', icon: ListChecks },
  { id: 'history', icon: HistoryIcon },
  { id: 'automations', icon: Bot },
  { id: 'tools', icon: Wrench },
]

/**
 * The BOOK: the desk folded into a rail beside the chat. Same components as
 * the full desk, one at a time behind a tab; drag its edge to size it,
 * collapse it to a spine when the conversation needs the room.
 */
export function Book({
  wallets,
  primary,
  provider,
  providerReady,
  unlocked,
  collapsed,
  cramped = false,
  width,
  onResize,
  onToggle,
  onOpenDesk,
  onOpenSettings,
  highlightOrder,
  entering = false,
  onReject,
  missions,
  sessionKey,
  onOpenSession,
}: {
  wallets: Wallet[]
  primary: string | null
  provider: ProviderId
  providerReady: boolean
  unlocked: boolean
  collapsed: boolean
  /**
   * The frame has no room for a split, so `onToggle` cannot open this panel
   * here however many times it is pressed. The spine offers the Desk instead.
   */
  cramped?: boolean
  width: number
  onResize: (width: number) => void
  onToggle: () => void
  /** Full-width instruments — the only place the BOOK fits on a narrow frame. */
  onOpenDesk?: () => void
  onOpenSettings: () => void
  highlightOrder: string | null
  /** The desk is powering on: the hero value counts up once. */
  entering?: boolean
  /**
   * The chat's reject path (it tells the agent why). Returns true when it
   * took the order; false leaves the plain decision to the BOOK.
   */
  onReject?: (order: Order) => boolean
  /**
   * The desk's one `useMissions` (it binds the cron events; a second would
   * double every write). The Automations tab lists and steers through it.
   */
  missions: MissionsApi
  /** The desk's chat: the tab names the automations filed by another one. */
  sessionKey: string
  onOpenSession: (sessionKey: string) => void
}) {
  const tab = useTradingUi((s) => s.bookTab)
  const setTab = useTradingUi((s) => s.setBookTab)
  // The tab strip scrolls when a narrow BOOK cannot fit it; a tab chosen from
  // outside (a Holdings row's Markets, a Swap request) is brought into view.
  const tabsRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const selected = tabsRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
    selected?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }, [tab, collapsed])
  // Labels go before a glyph would clip (History was cut mid-letter by the
  // pips at 520 px), and a strip that still overflows fades on the side
  // that hides tabs, so it reads as scrollable.
  useEffect(() => {
    const el = tabsRef.current
    if (!el) return
    return watchTabStrip(el)
  }, [collapsed])
  const openSheet = useTradingUi((s) => s.openSheet)
  const requestSwap = useTradingUi((s) => s.requestSwap)
  const openMarkets = useTradingUi((s) => s.openMarkets)
  const swapRequest = useTradingUi((s) => s.swapRequest)
  const clearSwapRequest = useTradingUi((s) => s.clearSwapRequest)
  const [walletSel, setWalletSel] = useState<string | 'all'>('all')
  const [picked, setPicked] = useState<Holding | null>(null)
  // A swap asked for from anywhere (a Holdings row, a Markets row, a chat
  // card) becomes this ticket's prefill; `requestSwap` has already put the
  // BOOK on its Swap tab. The request is cleared once taken.
  const [prefill, setPrefill] = useState<SwapPrefill | null>(null)
  if (swapRequest && swapRequest.seq !== prefill?.seq) setPrefill({ ...swapRequest })
  useEffect(() => {
    if (swapRequest) clearSwapRequest(swapRequest.seq)
  }, [swapRequest, clearSwapRequest])
  const [sheet, setSheet] = useState<WalletSheetMode | null>(null)
  const [inspect, setInspect] = useState<{ chainId: number; hash?: string } | null>(null)
  const now = useNow(30_000)
  const decide = useOrderDecision()
  const walletWrite = useWalletMutation()
  const chains = useTradingStatus().data?.chains ?? []

  const selected: string | 'all' =
    walletSel !== 'all' && !wallets.some((w) => sameAddress(w.address, walletSel))
      ? 'all'
      : walletSel
  const walletAddress = selected === 'all' ? undefined : selected
  const [showHidden, setShowHidden] = useState(false)
  const portfolio = usePortfolio(walletAddress, !collapsed, showHidden)
  const tokenVisibility = useTokenVisibility()
  const orders = useOrders(undefined, !collapsed && tab === 'orders')
  const history = useHistory(walletAddress, undefined, !collapsed && tab === 'history')
  const totals = portfolio.data?.totals ?? EMPTY_TOTALS
  const holdings = portfolio.data?.holdings ?? []
  const { counting, attach: attachCount } = useCountUp(
    totals.valueUsd,
    entering && !portfolio.isPending,
  )
  const pendingCount = orders.orders.filter(isAwaitingApproval).length
  // The Automations tab's badge: proposals waiting on the operator, whichever chat made them.
  const needsCount =
    missions.awaitingMandates.length +
    missions.awaitingTriggers.length +
    missions.awaitingBrackets.length
  // The hero is coloured by the book's own result — what was banked plus
  // what is still open — never by the day's price move, which is only the
  // chip's business.
  const tone = pnlTone((totals.realizedUsd ?? 0) + (totals.unrealizedUsd ?? 0))
  const deltaTone = pnlTone(totals.change24hUsd)
  const Arrow = deltaTone === 'down' ? TrendingDown : TrendingUp
  const unpriced = portfolio.data?.unpricedCount ?? 0
  const segments = allocationSegments(holdings)
  // The switcher names every wallet and what it holds. A portfolio scoped to
  // one wallet only carries that one, and a dropdown that showed one figure
  // beside "All wallets" would be lying, so the unscoped totals are fetched
  // exactly while a single wallet is selected.
  const allPortfolio = usePortfolio(undefined, !collapsed && selected !== 'all')
  const headTotals = useMemo(() => {
    const rows = (selected === 'all' ? portfolio.data : allPortfolio.data)?.wallets ?? []
    const m = new Map<string, Totals>()
    for (const row of rows) m.set(row.wallet.address.toLowerCase(), row.totals)
    return m
  }, [portfolio.data, allPortfolio.data, selected])

  // Drag the left edge; the width is measured from the rendered edge so a
  // concession-clamped panel does not jump on the first pixel.
  const dragRef = useRef<{ startX: number; startW: number } | null>(null)
  const onDragStart = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      dragRef.current = { startX: e.clientX, startW: width }
      e.currentTarget.setPointerCapture(e.pointerId)
      document.body.dataset.dragging = 'true'
    },
    [width],
  )
  const onDragMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const d = dragRef.current
      if (!d) return
      const next = Math.min(BOOK_MAX, Math.max(BOOK_MIN, d.startW + (d.startX - e.clientX)))
      onResize(next)
    },
    [onResize],
  )
  const onDragEnd = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    dragRef.current = null
    delete document.body.dataset.dragging
    try {
      e.currentTarget.releasePointerCapture(e.pointerId)
    } catch {
      /* already released */
    }
  }, [])

  // Touch ID, when it is on, is asked for inside the decision itself
  // (`useOrderDecision`); a declined prompt has toasted and sent nothing.
  async function onDecide(order: Order, approve: boolean): Promise<void> {
    // A rejection of this desk's own order goes the chat's way, so the
    // agent hears about it where it asked instead of finding a status flip.
    if (!approve && onReject?.(order)) return
    try {
      await decide.mutateAsync({ orderId: order.orderId, approve, order })
    } catch (err) {
      if (isTouchIdDeclined(err)) return
      toastOrder('error', order.orderId, `${t('trading.approvals.failed')}: ${errorText(err)}`)
      return
    }
    if (approve) toastOrderSending(order.orderId, t('trading.approvals.approved'))
    else toastOrderRejected(order.orderId, t('trading.approvals.rejected'))
  }

  if (collapsed) {
    // On a cramped frame every button on this rail did nothing. The open button
    // flipped a preference the concession chain overruled on the next render,
    // and `setBookTab` — which opens the panel as well as selecting a tab — was
    // overruled the same way. Both now offer the Desk, the only place the BOOK's
    // content fits at this width.
    const openHere = cramped && onOpenDesk ? onOpenDesk : onToggle
    const openLabel = cramped && onOpenDesk ? t('trading.book.openDesk') : t('trading.book.open')
    return (
      <aside className="trd-book trd-book--spine" aria-label={t('trading.book.title')}>
        <button
          type="button"
          className="trd-book__spinebtn app-no-drag"
          onClick={openHere}
          aria-label={openLabel}
          title={openLabel}
          data-testid="book-open"
        >
          {cramped && onOpenDesk ? (
            <Maximize2 className="size-4" strokeWidth={1.75} aria-hidden />
          ) : (
            <ChevronsLeft className="size-4" strokeWidth={1.75} aria-hidden />
          )}
        </button>
        {TABS.map(({ id, icon: Icon }) => (
          <button
            key={id}
            type="button"
            className="trd-book__spinebtn app-no-drag"
            onClick={() => {
              // `setBookTab` opens the panel itself; on a cramped frame that
              // open is overruled, so the tab has to name the Desk instead.
              setTab(id)
              if (cramped && onOpenDesk) onOpenDesk()
            }}
            aria-label={t(`trading.book.tab.${id}`)}
            title={t(`trading.book.tab.${id}`)}
            data-testid={`book-spine-${id}`}
          >
            <Icon className="size-4" strokeWidth={1.75} aria-hidden />
            {(id === 'orders' && pendingCount > 0) || (id === 'automations' && needsCount > 0) ? (
              <span className="trd-book__dot" aria-hidden />
            ) : null}
          </button>
        ))}
        <span className="trd-book__spinelabel">{t('trading.book.title')}</span>
      </aside>
    )
  }

  return (
    <aside
      className="trd-book"
      style={{ width }}
      aria-label={t('trading.book.title')}
      data-narrow={width < 440 || undefined}
      data-testid="book"
    >
      <div
        className="trd-book__handle"
        role="separator"
        aria-orientation="vertical"
        aria-label={t('trading.book.resize')}
        onPointerDown={onDragStart}
        onPointerMove={onDragMove}
        onPointerUp={onDragEnd}
        onPointerCancel={onDragEnd}
        onDoubleClick={() => onResize(360)}
      />
      <header className="trd-book__head">
        <div
          ref={tabsRef}
          className="trd-book__tabs"
          role="tablist"
          aria-label={t('trading.book.title')}
        >
          {TABS.map(({ id, icon: Icon }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              className="trd-book__tab app-no-drag"
              onClick={() => setTab(id)}
              title={t(`trading.book.tab.${id}`)}
              data-testid={`book-tab-${id}`}
            >
              <Icon className="size-3.5" strokeWidth={1.75} aria-hidden />
              <span>{t(`trading.book.tab.${id}`)}</span>
              {id === 'orders' && pendingCount > 0 ? (
                <span className="trd-book__count">{pendingCount}</span>
              ) : id === 'automations' && needsCount > 0 ? (
                <span className="trd-book__count">{needsCount}</span>
              ) : null}
            </button>
          ))}
        </div>
        <NetworkPips enabled={!collapsed} />
        <Button
          variant="ghost"
          size="icon"
          aria-label={t('trading.book.collapse')}
          title={t('trading.book.collapse')}
          onClick={onToggle}
          data-testid="book-collapse"
        >
          <ChevronsRight className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
        </Button>
      </header>

      <div className="trd-book__body" data-tab={tab}>
        <PanelBoundary key={tab} name={tab}>
          {tab === 'portfolio' ? (
            <div className="trd-book__portfolio">
              {/* The same head the full desk wears, compressed by the
                .trd-book overrides — one vocabulary, one set of rules. */}
              <section className="trd-hero trd-book__hero" data-tone={tone}>
                <WalletHead
                  compact
                  wallets={wallets}
                  selected={selected}
                  onSelect={setWalletSel}
                  totals={headTotals}
                  // Locking the vault is the full desk's rail; the head never
                  // raises it, and the BOOK has no sheet for it.
                  onAction={(action) => {
                    if (action.kind !== 'lock') setSheet(action)
                  }}
                  onSetPrimary={(w) =>
                    walletWrite.mutate(
                      { method: 'wallet.setPrimary', params: { address: w.address } },
                      {
                        onError: (err) =>
                          toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`),
                      },
                    )
                  }
                  onManage={() => setSheet({ kind: 'manage' })}
                  chains={chains}
                />
                <div className="trd-hero__figure">
                  <b data-testid="book-value">
                    {portfolio.isPending ? (
                      <span className="trd-skel" style={{ width: 120, height: 24 }} />
                    ) : counting ? (
                      <span className="trd-num" data-testid="book-value-counting" ref={attachCount}>
                        {formatUsd(0)}
                      </span>
                    ) : (
                      <Money value={totals.valueUsd} />
                    )}
                  </b>
                  {totals.change24hUsd !== null ? (
                    <span
                      className="trd-delta"
                      data-tone={deltaTone}
                      data-testid="book-delta"
                      aria-label={t('trading.overview.move24h')}
                      title={`${t('trading.overview.move24h')}: ${formatUsd(totals.change24hUsd, { signed: true })} ${t('trading.overview.today')}`}
                    >
                      {deltaTone !== 'flat' ? (
                        <Arrow className="size-3" strokeWidth={2.25} aria-hidden />
                      ) : null}
                      <Money value={totals.change24hUsd} signed cell />
                      <em className="trd-num">
                        {formatPct(totals.change24hPct, { signed: true })}
                      </em>
                    </span>
                  ) : null}
                  {unpriced > 0 ? (
                    <span className="trd-delta" data-tone="flat" data-testid="book-unpriced">
                      <em className="trd-num">{unpriced}</em> {t('trading.overview.unpriced')}
                    </span>
                  ) : null}
                </div>
                <div className="trd-hero__stats">
                  <Stat
                    label={t('trading.overview.unrealized')}
                    value={totals.unrealizedUsd}
                    toned
                  />
                  <Stat label={t('trading.overview.realized')} value={totals.realizedUsd} toned />
                  <Stat label={t('trading.overview.cost')} value={totals.costUsd} />
                  <Stat label={t('trading.overview.gas')} value={totals.gasUsd} />
                </div>
                <div className="trd-alloc">
                  <div className="trd-alloc__bar" aria-hidden>
                    {segments.map((s, i) => (
                      <span
                        key={s.key}
                        style={{ ['--i' as string]: i, flexBasis: `${s.pct}%` }}
                        data-other={s.symbol === 'other' ? 'true' : undefined}
                        title={`${s.symbol} ${formatPct(s.pct)}`}
                      />
                    ))}
                  </div>
                </div>
              </section>

              <Holdings
                holdings={holdings}
                loading={portfolio.isPending}
                error={portfolio.isError ? portfolio.error : undefined}
                onRetry={() => void portfolio.refetch()}
                wallets={wallets}
                selected={picked}
                onSelect={setPicked}
                showChain
                hiddenCount={portfolio.data?.hiddenCount ?? 0}
                showHidden={showHidden}
                hiddenLoading={showHidden && portfolio.isPlaceholderData}
                onToggleHidden={() => setShowHidden((v) => !v)}
                onSetHidden={(h, hidden) =>
                  tokenVisibility.mutate({ chainId: h.chainId, address: h.token.address, hidden })
                }
                onSwap={(h) =>
                  requestSwap({
                    chainId: h.chainId,
                    tokenIn: h.token,
                    wallet: h.wallet ?? walletAddress,
                  })
                }
                onMarkets={(h) =>
                  openMarkets({
                    chainId: h.chainId,
                    address: h.token.address,
                    symbol: h.token.symbol,
                  })
                }
              />
            </div>
          ) : tab === 'swap' ? (
            <SwapPanel
              wallets={wallets}
              primary={primary}
              selectedWallet={selected}
              provider={provider}
              providerReady={providerReady}
              onOpenSettings={onOpenSettings}
              unlocked={unlocked}
              prefill={prefill}
              onSent={() => setTab('orders')}
            />
          ) : tab === 'markets' ? (
            <Markets />
          ) : tab === 'orders' ? (
            <>
              <Orders
                orders={orders.orders}
                approvalsOnly={false}
                deciding={decide.isPending ? (decide.variables?.orderId ?? null) : null}
                onDecide={onDecide}
                showWallet={wallets.length > 1}
                highlight={highlightOrder}
                onInspect={(o) => o.txHash && setInspect({ chainId: o.chainId, hash: o.txHash })}
              />
            </>
          ) : tab === 'automations' ? (
            <Automations
              missions={missions}
              sessionKey={sessionKey}
              pendingApprovals={pendingCount}
              enabled={!collapsed}
              onOpenSession={onOpenSession}
            />
          ) : tab === 'tools' ? (
            <ToolsPanel
              wallet={walletAddress ?? primary ?? undefined}
              onSend={() => openSheet('send')}
              onMultisend={() => openSheet('multisend')}
              onInspect={() => openSheet('inspect')}
              onBurn={() => openSheet('burn')}
            />
          ) : (
            <History
              entries={history.entries}
              loading={history.isPending}
              now={now}
              showWallet={selected === 'all' && wallets.length > 1}
            />
          )}
        </PanelBoundary>
      </div>
      {sheet ? <WalletSheet mode={sheet} onClose={() => setSheet(null)} /> : null}
      {inspect ? (
        <DecodeSheet
          chainId={inspect.chainId}
          hash={inspect.hash}
          onClose={() => setInspect(null)}
        />
      ) : null}
    </aside>
  )
}

function Stat({ label, value, toned }: { label: string; value: number; toned?: boolean }) {
  return (
    <div className="trd-stat" title={formatUsd(value, { signed: toned })}>
      <span className="trd-stat__label">{label}</span>
      <span className="trd-stat__value">
        <Money value={value} signed={toned} toned={toned} cell />
      </span>
    </div>
  )
}
