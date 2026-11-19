import './trading.css'
import { CandlestickChart, KeyRound, Lock, Wallet as WalletIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { isTouchIdDeclined } from '~/lib/biometric-gate'
import { toastOrder, toastOrderRejected, toastOrderSending } from '~/lib/order-toasts'
import { useNow } from '~/lib/use-now'
import { useGateway } from '~/stores/gateway'
import {
  useHistory,
  useLimits,
  useLpBook,
  useLpRefresh,
  useLpWrite,
  useOrderDecision,
  useOrders,
  usePortfolio,
  useSync,
  useTokenVisibility,
  useTradingInvalidation,
  useTradingStatus,
  useWalletMutation,
  useWalletStatus,
  useWallets,
} from '~/stores/trading'
import { useTradingUi } from '~/stores/trading-ui'
import { useUi } from '~/stores/ui'
import { Notice } from '~/views/settings/parts'
import { History } from './History'
import { Holdings } from './Holdings'
import { Liquidity } from './Liquidity'
import { Markets } from './Markets'
import {
  EMPTY_TOTALS,
  errorText,
  filterHoldings,
  isAwaitingApproval,
  sameAddress,
  syncShown,
} from './logic'
import { Orders } from './Orders'
import { Overview } from './Overview'
import { ChainMark } from './ChainMark'
import { DecodeSheet } from './DecodeSheet'
import { Automations } from './desk/Automations'
import { forWallet } from './desk/automations-logic'
import type { MissionsApi } from './desk/missions'
import { ToolsPanel } from './desk/ToolsPanel'
import { PanelBoundary, Spinner } from './parts'
import { PriceChart } from './PriceChart'
import type { SwapPrefill } from './SwapPanel'
import { Ticket } from './Ticket'
import {
  CHAINS,
  DEFAULT_PROVIDER,
  type ChainId,
  type Holding,
  type Order,
  type ProviderId,
  type Totals,
  type Wallet,
} from './types'
import { useSwitchProvider } from './useSwitchProvider'
import { WalletHead } from './WalletHead'
import { Budget, WalletRail, type WalletAction, type WalletSelection } from './WalletRail'
import { WalletSheet, type WalletSheetMode } from './WalletSheet'

type Tab =
  | 'holdings'
  | 'liquidity'
  | 'markets'
  | 'history'
  | 'orders'
  | 'approvals'
  | 'automations'
  | 'tools'

/**
 * What the full desk needs from the frame to show the Automations tab: the
 * frame's one `useMissions` (a second would bind the cron events twice and
 * double every write), the desk's chat, and a way to open another one.
 */
export interface DeskAutomations {
  missions: MissionsApi
  sessionKey: string
  onOpenSession: (sessionKey: string) => void
}

/**
 * The desk. Three columns once the vault is open; before that, one message
 * and the single action that gets you to the next state: start the
 * gateway, turn trading on, create the vault, unlock it, add a wallet.
 */
export function TradingView({
  entering,
  automations,
}: {
  entering?: boolean
  /** The frame's missions; without them the Automations tab is not offered. */
  automations?: DeskAutomations
}) {
  const gatewayState = useGateway((s) => s.status.state)
  if (gatewayState !== 'running') {
    return (
      <div className="trd-state">
        <CandlestickChart className="trd-state__mark size-9" strokeWidth={1.25} aria-hidden />
        <h1>
          {gatewayState === 'starting'
            ? t('chat.waitingGateway')
            : t(`gateway.state.${gatewayState}`)}
        </h1>
        <p>{t('trading.offline.body')}</p>
      </div>
    )
  }
  return <Gate entering={Boolean(entering)} automations={automations} />
}

function Gate({ entering, automations }: { entering: boolean; automations?: DeskAutomations }) {
  useTradingInvalidation()
  const status = useTradingStatus()
  const vault = useWalletStatus()
  const openSettings = useUi((s) => s.openSettings)
  const [sheet, setSheet] = useState<WalletSheetMode | null>(null)

  // A failed status call is not "no vault yet": say the gateway did not
  // answer, and offer the one thing that helps — asking again.
  if (status.isError || vault.isError) {
    return (
      <State
        icon={
          <CandlestickChart className="trd-state__mark size-9" strokeWidth={1.25} aria-hidden />
        }
        title={t('trading.offline.title')}
        body={t('trading.offline.body')}
        action={
          <Button
            variant="primary"
            onClick={() => {
              void status.refetch()
              void vault.refetch()
            }}
            data-testid="trading-retry"
          >
            {t('trading.offline.retry')}
          </Button>
        }
      />
    )
  }
  // The first answer takes a moment after the gateway comes up: an empty
  // frame here reads as the desk being broken, so it says it is loading.
  if (status.isPending || vault.isPending) {
    return (
      <div className="trd-state" data-testid="trading-loading" aria-busy="true">
        <Spinner className="trd-state__mark size-6" />
        <p>{t('trading.loading')}</p>
      </div>
    )
  }
  if (status.data && !status.data.enabled) {
    return (
      <State
        icon={
          <CandlestickChart className="trd-state__mark size-9" strokeWidth={1.25} aria-hidden />
        }
        title={t('trading.disabled.title')}
        body={t('trading.disabled.body')}
        action={
          <Button variant="primary" onClick={() => openSettings('trading')}>
            {t('trading.openSettings')}
          </Button>
        }
      />
    )
  }
  const v = vault.data
  if (!v || !v.initialized) {
    return (
      <>
        <State
          icon={<Lock className="trd-state__mark size-9" strokeWidth={1.25} aria-hidden />}
          title={t('trading.setup.title')}
          body={t('trading.setup.body')}
          action={
            <Button
              variant="primary"
              onClick={() => setSheet({ kind: 'setup' })}
              data-testid="vault-setup"
            >
              {t('trading.setup.cta')}
            </Button>
          }
        />
        {sheet ? <WalletSheet mode={sheet} onClose={() => setSheet(null)} /> : null}
      </>
    )
  }
  if (!v.unlocked) {
    return (
      <>
        <State
          icon={<Lock className="trd-state__mark size-9" strokeWidth={1.25} aria-hidden />}
          title={t('trading.locked.title')}
          body={t('trading.locked.body')}
          action={
            <Button
              variant="primary"
              onClick={() => setSheet({ kind: 'unlock' })}
              data-testid="vault-unlock"
            >
              {t('trading.locked.cta')}
            </Button>
          }
        />
        {sheet ? <WalletSheet mode={sheet} onClose={() => setSheet(null)} /> : null}
      </>
    )
  }
  const provider = status.data?.provider ?? DEFAULT_PROVIDER
  return (
    <Desk
      entering={entering}
      automations={automations}
      provider={provider}
      // Only Uniswap needs a key; the aggregator is ready as soon as it answers.
      providerReady={provider !== 'uniswap' || Boolean(status.data?.apiKeyConfigured)}
      needsKey={provider === 'uniswap' && !status.data?.apiKeyConfigured}
    />
  )
}

function State({
  icon,
  title,
  body,
  action,
}: {
  icon: React.ReactNode
  title: string
  body: string
  action: React.ReactNode
}) {
  return (
    <div className="trd-state">
      {icon}
      <h1>{title}</h1>
      <p>{body}</p>
      <div className="trd-state__actions">{action}</div>
    </div>
  )
}

function Desk({
  provider,
  providerReady,
  needsKey,
  entering,
  automations,
}: {
  provider: ProviderId
  providerReady: boolean
  needsKey: boolean
  /** The mode switch is playing: the head counts its value up from zero. */
  entering: boolean
  automations?: DeskAutomations
}) {
  const status = useTradingStatus()
  const vault = useWalletStatus()
  const { wallets, primary, isPending: walletsPending } = useWallets()
  const [chosen, setSelected] = useState<WalletSelection>('all')
  const [chain, setChain] = useState<ChainId | null>(null)
  const [tab, setTab] = useState<Tab>('holdings')
  const [picked, setPicked] = useState<Holding | null>(null)
  const requestSwap = useTradingUi((s) => s.requestSwap)
  const openMarkets = useTradingUi((s) => s.openMarkets)
  const swapRequest = useTradingUi((s) => s.swapRequest)
  const clearSwapRequest = useTradingUi((s) => s.clearSwapRequest)
  const openSheet = useTradingUi((s) => s.openSheet)
  const setDeskMode = useTradingUi((s) => s.setDeskMode)
  const [inspecting, setInspecting] = useState(false)
  // A swap asked for from anywhere (a Holdings row, a Markets row) fills the
  // ticket, which is always on screen here; the request is cleared once taken.
  const [prefill, setPrefill] = useState<SwapPrefill | null>(null)
  if (swapRequest && swapRequest.seq !== prefill?.seq) setPrefill({ ...swapRequest })
  useEffect(() => {
    if (swapRequest) clearSwapRequest(swapRequest.seq)
  }, [swapRequest, clearSwapRequest])
  const [sheet, setSheet] = useState<WalletSheetMode | null>(null)
  const [highlight, setHighlight] = useState<string | null>(null)
  const [railWanted, setRailWanted] = useRailPreference()
  const openSettings = useUi((s) => s.openSettings)
  const now = useNow(30_000)
  const location = useLocation()
  const navigate = useNavigate()

  // A notification about an order lands on the approvals tab with that row
  // in view; the URL is then cleaned so a reload does not repeat it.
  const orderParam = new URLSearchParams(location.search).get('order')
  const [seenOrder, setSeenOrder] = useState<string | null>(null)
  if (orderParam && orderParam !== seenOrder) {
    setSeenOrder(orderParam)
    setTab('approvals')
    setHighlight(orderParam)
    // The order may be another wallet's; a BOOK scoped elsewhere would hide it.
    setSelected('all')
  }
  useEffect(() => {
    if (orderParam) void navigate('/trading', { replace: true })
  }, [orderParam, navigate])

  // The rail's selection must be a wallet that still exists.
  const selected: WalletSelection =
    chosen !== 'all' && wallets.length > 0 && !wallets.some((w) => sameAddress(w.address, chosen))
      ? 'all'
      : chosen

  const walletAddress = selected === 'all' ? undefined : selected
  const [showHidden, setShowHidden] = useState(false)
  const portfolio = usePortfolio(walletAddress, true, showHidden)
  const tokenVisibility = useTokenVisibility()
  const orders = useOrders(undefined, true, 50, walletAddress)
  const history = useHistory(walletAddress, chain ?? undefined, tab === 'history')
  const limitsWallet: Wallet | null =
    wallets.find((w) => (selected === 'all' ? w.primary : sameAddress(w.address, selected))) ??
    wallets[0] ??
    null
  const limits = useLimits(limitsWallet?.address ?? null)
  const decide = useOrderDecision()
  const sync = useSync()
  const walletWrite = useWalletMutation()
  const switchProvider = useSwitchProvider()

  const totalsByWallet = useMemo(() => {
    const m = new Map<string, Totals>()
    for (const row of portfolio.data?.wallets ?? [])
      m.set(row.wallet.address.toLowerCase(), row.totals)
    return m
  }, [portfolio.data])
  // The rail always shows every wallet's total, whichever one is selected.
  const allPortfolio = usePortfolio(undefined, selected !== 'all')
  const allTotals = useTotalsMap(allPortfolio.data?.wallets)
  const railTotals = selected === 'all' ? totalsByWallet : allTotals

  const holdings = useMemo(
    () => filterHoldings(portfolio.data?.holdings ?? [], chain),
    [portfolio.data, chain],
  )
  // The engine reads LP positions only while someone asks: off this tab, nothing is asked.
  const lpBook = useLpBook(walletAddress, chain ?? undefined, tab === 'liquidity')
  const lpRefresh = useLpRefresh()
  const lpWrite = useLpWrite()
  const [lpActing, setLpActing] = useState<string | null>(null)
  // Token art for the pairs, from what the wallets hold.
  const logos = useMemo(() => {
    const m = new Map<string, string>()
    for (const h of portfolio.data?.holdings ?? [])
      if (h.token.logoUrl) m.set(h.token.address.toLowerCase(), h.token.logoUrl)
    return m
  }, [portfolio.data])
  const pending = orders.orders.filter(isAwaitingApproval).length
  // The Automations tab's badge: proposals waiting on the operator, whichever chat made them,
  // for the selected wallet when one is.
  const needsCount = automations
    ? forWallet(automations.missions.awaitingMandates, walletAddress).length +
      forWallet(automations.missions.awaitingTriggers, walletAddress).length +
      forWallet(automations.missions.awaitingBrackets, walletAddress).length
    : 0
  const tabs: Tab[] = [
    'holdings',
    'markets',
    'liquidity',
    'history',
    'orders',
    'approvals',
    ...(automations ? (['automations'] as const) : []),
    'tools',
  ]
  // Send, Multisend and Burn post their ask into the desk's chat, which owns
  // those sheets: leave the full desk for it with the sheet already asked for.
  function openChatSheet(sheet: 'send' | 'multisend' | 'burn') {
    openSheet(sheet)
    setDeskMode(false)
  }

  // Touch ID, when it is on, is asked for inside the decision itself
  // (`useOrderDecision`); a declined prompt has toasted and sent nothing.
  async function onDecide(order: Order, approve: boolean): Promise<void> {
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

  function onWalletAction(action: WalletAction) {
    if (action.kind === 'lock') {
      walletWrite.mutate(
        { method: 'wallet.lock', params: {} },
        { onError: (err) => toast.error(`${t('trading.error.lock')}: ${errorText(err)}`) },
      )
      return
    }
    setSheet(action)
  }

  function onSync() {
    sync.mutate(walletAddress ? { wallet: walletAddress } : {}, {
      onError: (err) => toast.error(`${t('trading.error.sync')}: ${errorText(err)}`),
    })
  }

  function onSetPrimary(wallet: Wallet) {
    walletWrite.mutate(
      { method: 'wallet.setPrimary', params: { address: wallet.address } },
      { onError: (err) => toast.error(`${t('trading.sheet.error')}: ${errorText(err)}`) },
    )
  }

  if (!walletsPending && wallets.length === 0) {
    return (
      <>
        <State
          icon={<WalletIcon className="trd-state__mark size-9" strokeWidth={1.25} aria-hidden />}
          title={t('trading.noWallets.title')}
          body={t('trading.noWallets.body')}
          action={
            <>
              <Button onClick={() => setSheet({ kind: 'import' })}>
                {t('trading.noWallets.import')}
              </Button>
              <Button
                variant="primary"
                onClick={() => setSheet({ kind: 'create' })}
                data-testid="wallet-create"
              >
                {t('trading.noWallets.create')}
              </Button>
            </>
          }
        />
        {sheet ? <WalletSheet mode={sheet} onClose={() => setSheet(null)} /> : null}
      </>
    )
  }

  const showWallet = selected === 'all'
  // The chain the inspector opens on: the primary wallet's first, else the
  // engine's first — the same pick the chat's Inspect makes.
  const primaryWallet = wallets.find((w) => sameAddress(w.address, primary)) ?? wallets[0] ?? null
  const inspectChain = primaryWallet?.chains?.[0] ?? status.data?.chains?.[0]?.chainId ?? 8453
  const totals = portfolio.data?.totals ?? EMPTY_TOTALS
  // A list of one is not a list: with a single wallet the head says everything
  // the rail would, and the column is 232 px the ledger can have instead.
  const railOpen = railWanted && wallets.length > 1

  function onSelectWallet(s: WalletSelection) {
    setSelected(s)
    setPicked(null)
  }

  return (
    <div className="trd-viewport">
      <div className="trd" data-testid="trading-desk" data-rail={railOpen ? 'on' : 'off'}>
        {railOpen ? (
          <WalletRail
            wallets={wallets}
            totals={railTotals}
            selected={selected}
            onSelect={onSelectWallet}
            onAction={onWalletAction}
            onSetPrimary={onSetPrimary}
            limits={limits.data}
            limitsWallet={limitsWallet}
            chains={status.data?.chains ?? []}
            manualUnlock={vault.data?.unlockMode === 'manual'}
          />
        ) : null}

        <div className="trd-desk">
          {needsKey ? (
            <div className="trd-desk__notice">
              <Notice
                tone="info"
                action={
                  <Button onClick={() => openSettings('trading')} data-testid="add-key">
                    <KeyRound className="size-3.5" strokeWidth={1.75} aria-hidden />
                    {t('trading.noKey.cta')}
                  </Button>
                }
              >
                <b>{t('trading.noKey.title')}</b> {t('trading.noKey.body')}
              </Notice>
            </div>
          ) : null}

          <Overview
            totals={totals}
            holdings={holdings}
            unpricedCount={portfolio.data?.unpricedCount ?? 0}
            // The engine's routine pass is not shown: with many wallets it
            // runs most of the time. A resync shows from the click on.
            syncing={sync.isPending || syncShown(portfolio.data) || syncShown(status.data)}
            lastSyncAt={status.data?.lastSyncAt ?? null}
            now={now}
            loading={portfolio.isPending}
            onSync={onSync}
            // A switch lands in trading.status, which the ticket's provider
            // and its key gate are read from too: both follow it at once.
            provider={provider}
            providers={status.data?.providers ?? []}
            switchingProvider={switchProvider.switching}
            onSwitchProvider={switchProvider.switchTo}
            onOpenSettings={() => openSettings('trading')}
            entering={entering}
            head={
              <WalletHead
                wallets={wallets}
                selected={selected}
                onSelect={onSelectWallet}
                totals={railTotals}
                onAction={onWalletAction}
                onSetPrimary={onSetPrimary}
                onManage={() => setSheet({ kind: 'manage' })}
                chains={status.data?.chains ?? []}
                railOpen={railOpen}
                onToggleRail={() => setRailWanted(!railWanted)}
              />
            }
          />

          {/* The agent's budget rides in the rail; with the rail away it still
              has to be on screen, so it lays itself out as a strip instead. */}
          {!railOpen && limits.data && limitsWallet ? (
            <Budget limits={limits.data} wallet={limitsWallet} strip />
          ) : null}

          <div className="trd-panel">
            <div className="trd-tabs" role="tablist" aria-label={t('trading.title')}>
              {tabs.map((id) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={tab === id}
                  className="trd-tab app-no-drag"
                  data-testid={`tab-${id}`}
                  onClick={() => setTab(id)}
                >
                  {t(`trading.tab.${id}`)}
                  {id === 'approvals' && pending > 0 ? (
                    <span className="trd-tab__count">{pending}</span>
                  ) : id === 'automations' && needsCount > 0 ? (
                    <span className="trd-tab__count">{needsCount}</span>
                  ) : null}
                </button>
              ))}
              <span className="trd-tabs__spacer" />
              {/* Automations and Tools are not filtered by chain. */}
              <div
                className="trd-tabs__tools"
                data-off={tab === 'automations' || tab === 'tools' || undefined}
              >
                <div
                  role="radiogroup"
                  aria-label={t('trading.overview.chains')}
                  className="mac-segmented trd-chainpick"
                >
                  <button
                    type="button"
                    role="radio"
                    aria-checked={chain === null}
                    className="mac-segment app-no-drag"
                    onClick={() => setChain(null)}
                  >
                    {t('trading.overview.chains.all')}
                  </button>
                  {CHAINS.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      role="radio"
                      aria-checked={chain === c.id}
                      className="mac-segment app-no-drag"
                      onClick={() => setChain(c.id)}
                    >
                      <ChainMark chainId={c.id} />
                      <span data-long>{c.short}</span>
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="trd-panel__body" data-tab={tab}>
              <PanelBoundary key={tab} name={tab}>
                {tab === 'holdings' ? (
                  <>
                    {picked ? (
                      <PriceChart holding={picked} onClose={() => setPicked(null)} />
                    ) : null}
                    <Holdings
                      holdings={holdings}
                      loading={portfolio.isPending}
                      error={portfolio.isError ? portfolio.error : undefined}
                      onRetry={() => void portfolio.refetch()}
                      wallets={wallets}
                      selected={picked}
                      onSelect={setPicked}
                      showChain={chain === null}
                      hiddenCount={portfolio.data?.hiddenCount ?? 0}
                      showHidden={showHidden}
                      hiddenLoading={showHidden && portfolio.isPlaceholderData}
                      onToggleHidden={() => setShowHidden((v) => !v)}
                      onSetHidden={(h, hidden) =>
                        tokenVisibility.mutate(
                          {
                            chainId: h.chainId,
                            address: h.token.address,
                            hidden,
                          },
                          {
                            onError: (err) =>
                              toast.error(
                                `${t('trading.error.tokenVisibility')}: ${errorText(err)}`,
                              ),
                          },
                        )
                      }
                      onSwap={(h) =>
                        requestSwap({
                          chainId: h.chainId,
                          tokenIn: h.token,
                          wallet: h.wallet ?? (selected === 'all' ? undefined : selected),
                        })
                      }
                      onMarkets={(h) => {
                        openMarkets({
                          chainId: h.chainId,
                          address: h.token.address,
                          symbol: h.token.symbol,
                        })
                        setTab('markets')
                      }}
                    />
                  </>
                ) : tab === 'liquidity' ? (
                  <Liquidity
                    book={lpBook.data}
                    loading={lpBook.isPending}
                    error={lpBook.isError ? lpBook.error : undefined}
                    onRetry={() => void lpBook.refetch()}
                    onRefresh={() =>
                      lpRefresh.mutate(
                        { wallet: walletAddress, chainId: chain ?? undefined },
                        {
                          onError: (err) =>
                            toast.error(`${t('trading.error.title')}: ${errorText(err)}`),
                        },
                      )
                    }
                    refreshing={lpRefresh.isPending}
                    wallets={wallets}
                    showChain={chain === null}
                    logos={logos}
                    now={now}
                    acting={lpWrite.isPending ? lpActing : null}
                    onAction={(position, action) => {
                      if (!position.tokenId) return
                      setLpActing(position.id)
                      lpWrite.mutate(
                        { action, chainId: position.chain.id, tokenId: position.tokenId },
                        {
                          // The order's own card says what moves; approving it is there.
                          onSuccess: ({ order }) => {
                            toast.success(t('trading.liquidity.parked'))
                            setHighlight(order.orderId)
                            setTab('approvals')
                          },
                          onError: (err) =>
                            toast.error(
                              `${t('trading.liquidity.actionFailed')}: ${errorText(err)}`,
                            ),
                        },
                      )
                    }}
                  />
                ) : tab === 'markets' ? (
                  <Markets deskChain={chain} />
                ) : tab === 'automations' && automations ? (
                  <Automations
                    missions={automations.missions}
                    sessionKey={automations.sessionKey}
                    pendingApprovals={pending}
                    enabled
                    onOpenSession={automations.onOpenSession}
                    wallet={walletAddress}
                    primary={primary}
                    wallets={wallets}
                  />
                ) : tab === 'tools' ? (
                  <ToolsPanel
                    wallet={walletAddress ?? primary ?? undefined}
                    onSend={() => openChatSheet('send')}
                    onMultisend={() => openChatSheet('multisend')}
                    onInspect={() => setInspecting(true)}
                    onBurn={() => openChatSheet('burn')}
                  />
                ) : tab === 'history' ? (
                  <History
                    entries={history.entries}
                    loading={history.isPending}
                    error={history.isError ? history.error : undefined}
                    onRetry={() => void history.refetch()}
                    nextBefore={history.nextBefore}
                    wallet={walletAddress}
                    chainId={chain ?? undefined}
                    now={now}
                    showWallet={showWallet}
                  />
                ) : (
                  <Orders
                    orders={orders.orders}
                    approvalsOnly={tab === 'approvals'}
                    deciding={decide.isPending ? (decide.variables?.orderId ?? null) : null}
                    onDecide={onDecide}
                    showWallet={showWallet}
                    highlight={highlight}
                    onHighlighted={() => setHighlight(null)}
                    error={orders.isError ? orders.error : undefined}
                    onRetry={() => void orders.refetch()}
                  />
                )}
              </PanelBoundary>
            </div>
          </div>
        </div>

        <Ticket
          wallets={wallets}
          primary={primary}
          selectedWallet={selected}
          provider={provider}
          providerReady={providerReady}
          onOpenSettings={() => openSettings('trading')}
          unlocked={Boolean(vault.data?.unlocked)}
          prefill={prefill}
          onSent={() => setTab('orders')}
        />

        {sheet ? <WalletSheet mode={sheet} onClose={() => setSheet(null)} /> : null}
        {inspecting ? (
          <DecodeSheet chainId={inspectChain} onClose={() => setInspecting(false)} />
        ) : null}
      </div>
    </div>
  )
}

/** Where the rail's shown/hidden choice is kept. */
const RAIL_KEY = 'agentos.trading.rail'

/**
 * Whether the wallet rail is wanted, remembered across visits.
 *
 * Showing it again every time the desk mounts would undo the choice on each
 * trip through chat, which reads as the toggle not working. localStorage
 * rather than the gateway: it is this window's layout, not an account setting.
 */
function useRailPreference(): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(() => localStorage.getItem(RAIL_KEY) !== 'off')
  return [
    open,
    (next: boolean) => {
      setOpen(next)
      localStorage.setItem(RAIL_KEY, next ? 'on' : 'off')
    },
  ]
}

function useTotalsMap(rows: { wallet: Wallet; totals: Totals }[] | undefined): Map<string, Totals> {
  return useMemo(() => {
    const m = new Map<string, Totals>()
    for (const row of rows ?? []) m.set(row.wallet.address.toLowerCase(), row.totals)
    return m
  }, [rows])
}
