import './trading.css'
import { CandlestickChart, KeyRound, Lock, Wallet as WalletIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useNow } from '~/lib/use-now'
import { useGateway } from '~/stores/gateway'
import {
  useHistory,
  useLimits,
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
import { useUi } from '~/stores/ui'
import { Notice } from '~/views/settings/parts'
import { History } from './History'
import { Holdings } from './Holdings'
import { EMPTY_TOTALS, errorText, filterHoldings, isAwaitingApproval, sameAddress } from './logic'
import { Orders } from './Orders'
import { Overview } from './Overview'
import { ChainMark } from './ChainMark'
import { Spinner } from './parts'
import { PriceChart } from './PriceChart'
import { SwapPanel, type SwapPrefill } from './SwapPanel'
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

type Tab = 'holdings' | 'history' | 'orders' | 'approvals'

/**
 * The desk. Three columns once the vault is open; before that, one message
 * and the single action that gets you to the next state: start the
 * gateway, turn trading on, create the vault, unlock it, add a wallet.
 */
export function TradingView({ entering }: { entering?: boolean }) {
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
  return <Gate entering={Boolean(entering)} />
}

function Gate({ entering }: { entering: boolean }) {
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
}: {
  provider: ProviderId
  providerReady: boolean
  needsKey: boolean
  /** The mode switch is playing: the head counts its value up from zero. */
  entering: boolean
}) {
  const status = useTradingStatus()
  const vault = useWalletStatus()
  const { wallets, primary, isPending: walletsPending } = useWallets()
  const [chosen, setSelected] = useState<WalletSelection>('all')
  const [chain, setChain] = useState<ChainId | null>(null)
  const [tab, setTab] = useState<Tab>('holdings')
  const [picked, setPicked] = useState<Holding | null>(null)
  const [prefill, setPrefill] = useState<SwapPrefill | null>(null)
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
  const orders = useOrders(undefined)
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
  const pending = orders.orders.filter(isAwaitingApproval).length

  function onDecide(order: Order, approve: boolean) {
    decide.mutate(
      { orderId: order.orderId, approve },
      {
        onSuccess: () =>
          toast.success(
            approve ? t('trading.approvals.approved') : t('trading.approvals.rejected'),
            {
              id: `trd-order-${order.orderId}`,
            },
          ),
        onError: (err) =>
          toast.error(`${t('trading.approvals.failed')}: ${errorText(err)}`, {
            id: `trd-order-${order.orderId}`,
          }),
      },
    )
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
            syncing={Boolean(portfolio.data?.syncing || status.data?.syncing)}
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
              {(['holdings', 'history', 'orders', 'approvals'] as Tab[]).map((id) => (
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
                  ) : null}
                </button>
              ))}
              <span className="trd-tabs__spacer" />
              <div className="trd-tabs__tools">
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
              {tab === 'holdings' ? (
                <>
                  {picked ? <PriceChart holding={picked} onClose={() => setPicked(null)} /> : null}
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
                            toast.error(`${t('trading.error.tokenVisibility')}: ${errorText(err)}`),
                        },
                      )
                    }
                    onSwap={(h) =>
                      setPrefill({
                        chainId: h.chainId,
                        tokenIn: h.token,
                        wallet: h.wallet ?? (selected === 'all' ? undefined : selected),
                        seq: Date.now(),
                      })
                    }
                  />
                </>
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
            </div>
          </div>
        </div>

        <SwapPanel
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
