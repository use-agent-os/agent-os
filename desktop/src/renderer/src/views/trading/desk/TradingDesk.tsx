import './desk.css'
import { CandlestickChart, Lock, Unplug, Wallet as WalletIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { Button } from '~/components/ui/button'
import { sessionPath } from '~/components/sidebar/SessionRow'
import { t } from '~/i18n'
import { useTradingUi, type BookTab } from '~/stores/trading-ui'
import {
  useLimits,
  usePendingApprovals,
  useTradingInvalidation,
  useTradingStatus,
  useWalletStatus,
  useWallets,
} from '~/stores/trading'
import { useUi } from '~/stores/ui'
import { sameAddress } from '../logic'
import { DEFAULT_PROVIDER, type Order, type ProviderId } from '../types'
import { WalletSheet, type WalletSheetMode } from '../WalletSheet'
import { Book } from './Book'
import { bookConcession } from './desk-logic'
import { useMissions } from './missions'
import type { DeskMode } from './mode-logic'
import { StatusStrip } from './StatusStrip'
import type { DeskProps } from './useDeskInstruments'
import { useTradingSession } from './useTradingSession'

/**
 * Everything Trading mode adds around the chat, as one hook so the session
 * route can keep ONE tree in both modes — the ChatView (and its composer)
 * stays the same node across the switch; only what surrounds it changes.
 * With `active === false` every query is off and every slot is null.
 *
 * The gates before the desk — trading off, vault to set up or unlock, no
 * wallet — are one banner over the chat with the one action that gets you
 * on; the chat itself stays usable underneath.
 */
export function useDeskFrame(input: {
  sessionKey: string
  /** The mode the route derived; the strip's pill shows it even while the desk is off. */
  mode: DeskMode
  /** Trading mode with the gateway up: queries run, slots render. */
  active: boolean
  entering: boolean
  onSwitchMode: (next: DeskMode) => void
  onSessionPending: (count: number) => void
  /** Chat mode: the strip's slot for the chat's session actions (StatusStrip). */
  sessionSlot?: (el: HTMLDivElement | null) => void
}): {
  strip: ReactNode
  banner: ReactNode
  desk: DeskProps | null
  book: ReactNode
  /** The full-width desk replaces chat + BOOK (the strip's Desk toggle, `?desk=1`). */
  fullDesk: boolean
  sheet: ReactNode
  frameRef: React.RefObject<HTMLDivElement | null>
  collapsed: boolean
} {
  const { sessionKey, mode, active, entering, onSwitchMode, onSessionPending, sessionSlot } = input
  // An ordinary chat must not poll the trading engine or wake on its events.
  useTradingInvalidation(active)
  const status = useTradingStatus(active)
  const vault = useWalletStatus(active)
  const openSettings = useUi((s) => s.openSettings)
  const location = useLocation()
  const navigate = useNavigate()
  const [sheet, setSheet] = useState<WalletSheetMode | null>(null)

  const disabled = Boolean(status.data && !status.data.enabled)
  const v = vault.data
  const vaultReady = Boolean(v && v.initialized && v.unlocked)
  const locked = Boolean(v && v.initialized && !v.unlocked)
  const ready = active && !disabled && vaultReady
  const provider: ProviderId = status.data?.provider ?? DEFAULT_PROVIDER
  // Only Uniswap needs a key; the aggregator is ready as soon as it answers.
  const providerReady = provider !== 'uniswap' || Boolean(status.data?.apiKeyConfigured)
  const needsKey = provider === 'uniswap' && !status.data?.apiKeyConfigured
  const chains = useMemo(
    () => (status.data?.chains ?? []).map((c) => c.chainId),
    [status.data?.chains],
  )
  const engineLimits = status.data?.limits ?? null

  const { wallets, primary, isPending: walletsPending } = useWallets(ready)
  const fullDesk = useTradingUi((s) => s.deskMode)
  const setDeskMode = useTradingUi((s) => s.setDeskMode)
  const bookWidth = useTradingUi((s) => s.bookWidth)
  const bookOpen = useTradingUi((s) => s.bookOpen)
  const setBookWidth = useTradingUi((s) => s.setBookWidth)
  const toggleBook = useTradingUi((s) => s.toggleBook)
  const setBookOpen = useTradingUi((s) => s.setBookOpen)
  const setBookTab = useTradingUi((s) => s.setBookTab)

  // `?desk=1` opens the full desk; the strip toggle flips it and cleans the URL.
  const deskParam = active ? new URLSearchParams(location.search).get('desk') : null
  useEffect(() => {
    if (deskParam === '1') {
      setDeskMode(true)
      void navigate(sessionPath(sessionKey), { replace: true })
    }
  }, [deskParam, setDeskMode, navigate, sessionKey])

  const primaryWallet = wallets.find((w) => sameAddress(w.address, primary)) ?? wallets[0] ?? null
  const limits = useLimits(ready ? (primaryWallet?.address ?? null) : null)
  const sessionCtx = useMemo(
    () => ({
      wallets,
      chains,
      limits: engineLimits
        ? { thresholdUsd: engineLimits.approvalThresholdUsd, dailyCapUsd: engineLimits.dailyCapUsd }
        : null,
    }),
    [wallets, chains, engineLimits],
  )
  const session = useTradingSession(sessionCtx, active)
  // The one `useMissions` for the desk: the strip reads it here and the
  // chat's instruments get it through `desk`.
  const missions = useMissions(sessionKey, active)
  const globalPending = usePendingApprovals(active)
  const [streaming, setStreaming] = useState(false)
  const [sessionPending, setSessionPending] = useState(0)
  const reportPending = useCallback(
    (n: number) => {
      setSessionPending(n)
      onSessionPending(n)
    },
    [onSessionPending],
  )

  // Concession chain: the frame measures itself; the chat never yields its floor.
  const frameRef = useRef<HTMLDivElement>(null)
  const [frameWidth, setFrameWidth] = useState(0)
  useEffect(() => {
    const node = frameRef.current
    if (!node || !active) return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0
      setFrameWidth(w)
    })
    ro.observe(node)
    setFrameWidth(node.getBoundingClientRect().width)
    return () => ro.disconnect()
  }, [active, fullDesk])
  const concession = bookConcession(frameWidth || 9999, bookWidth, bookOpen)

  const openBookTab = useCallback(
    (tab: BookTab) => {
      setBookTab(tab)
      setBookOpen(true)
    },
    [setBookTab, setBookOpen],
  )

  // The chat's reject path, lent to the BOOK: a rejection from the Orders
  // tab then tells the agent why, the same as one from the card.
  const rejectRef = useRef<((order: Order, reason: string) => void) | null>(null)
  const bindReject = useCallback((fn: ((order: Order, reason: string) => void) | null) => {
    rejectRef.current = fn
  }, [])
  const rejectFromBook = useCallback(
    (order: Order): boolean => {
      const fn = rejectRef.current
      // Only this desk's own orders get the message; another session's agent
      // would never read it here.
      if (!fn || order.sessionKey !== sessionKey) return false
      fn(order, '')
      return true
    },
    [sessionKey],
  )

  // "Start fresh" mints a new session key, and missions are filed by key: a
  // mission left enabled on the old key would keep trading, unseen. Every
  // active mission is paused first; if one refuses, the desk stays here.
  const { startFresh: mintFresh } = session
  const { pauseAll } = missions
  const startFresh = useCallback(() => {
    void pauseAll().then((ok) => {
      if (ok) mintFresh()
    })
  }, [pauseAll, mintFresh])

  if (!active) {
    return {
      strip: <StatusStrip mode={mode} onSwitchMode={onSwitchMode} sessionSlot={sessionSlot} />,
      banner: null,
      desk: null,
      book: null,
      fullDesk: false,
      sheet: null,
      frameRef,
      collapsed: false,
    }
  }

  let banner: ReactNode = null
  if (status.isError || vault.isError) {
    // No answer is not "no vault": say so, and offer to ask again.
    banner = (
      <GateBanner
        icon={<Unplug className="size-4" strokeWidth={1.75} aria-hidden />}
        title={t('trading.offline.title')}
        body={t('trading.offline.body')}
        action={
          <Button
            variant="primary"
            onClick={() => {
              void status.refetch()
              void vault.refetch()
            }}
            data-testid="desk-retry"
          >
            {t('trading.offline.retry')}
          </Button>
        }
      />
    )
  } else if (disabled) {
    banner = (
      <GateBanner
        icon={<CandlestickChart className="size-4" strokeWidth={1.75} aria-hidden />}
        title={t('trading.disabled.title')}
        body={t('trading.disabled.body')}
        action={
          <Button variant="primary" onClick={() => openSettings('trading')}>
            {t('trading.openSettings')}
          </Button>
        }
      />
    )
  } else if (!status.isPending && !vault.isPending && !vaultReady) {
    banner = (
      <GateBanner
        icon={<Lock className="size-4" strokeWidth={1.75} aria-hidden />}
        title={locked ? t('trading.locked.title') : t('trading.setup.title')}
        body={locked ? t('trading.locked.body') : t('trading.setup.body')}
        action={
          <Button
            variant="primary"
            onClick={() => setSheet({ kind: locked ? 'unlock' : 'setup' })}
            data-testid={locked ? 'vault-unlock' : 'vault-setup'}
          >
            {locked ? t('trading.locked.cta') : t('trading.setup.cta')}
          </Button>
        }
      />
    )
  } else if (ready && !walletsPending && wallets.length === 0) {
    banner = (
      <GateBanner
        icon={<WalletIcon className="size-4" strokeWidth={1.75} aria-hidden />}
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
    )
  }
  const bookReady = ready && wallets.length > 0

  const desk: DeskProps = {
    entering,
    wallets,
    primary,
    limits: limits.data ?? null,
    gate: { needsKey, provider },
    missions,
    onFirstSend: session.ensureFiled,
    onStartFresh: startFresh,
    onOpenBookTab: openBookTab,
    onStreaming: setStreaming,
    onSessionPending: reportPending,
    onBindReject: bindReject,
  }

  return {
    strip: (
      <StatusStrip
        mode="trading"
        onSwitchMode={onSwitchMode}
        missions={missions.missions}
        running={missions.running}
        sessionPending={sessionPending}
        globalPending={globalPending > 0 ? globalPending : null}
        streaming={streaming}
        deskMode={fullDesk}
        onToggleDesk={() => setDeskMode(!fullDesk)}
        onOpenApprovals={() => {
          if (fullDesk) return
          openBookTab('orders')
        }}
      />
    ),
    banner,
    desk,
    book: bookReady ? (
      <Book
        wallets={wallets}
        primary={primary}
        provider={provider}
        providerReady={providerReady}
        unlocked={Boolean(vault.data?.unlocked)}
        collapsed={concession.collapsed}
        cramped={concession.cramped}
        width={concession.book}
        onResize={setBookWidth}
        onToggle={toggleBook}
        onOpenDesk={() => setDeskMode(true)}
        onOpenSettings={() => openSettings('trading')}
        highlightOrder={null}
        entering={entering}
        onReject={rejectFromBook}
      />
    ) : null,
    fullDesk,
    sheet: sheet ? <WalletSheet mode={sheet} onClose={() => setSheet(null)} /> : null,
    frameRef,
    collapsed: concession.collapsed,
  }
}

function GateBanner({
  icon,
  title,
  body,
  action,
}: {
  icon: ReactNode
  title: string
  body: string
  action: ReactNode
}) {
  return (
    <div className="trd-gate" role="status" data-testid="desk-gate">
      <span className="trd-gate__mark">{icon}</span>
      <div className="trd-gate__text">
        <b>{title}</b>
        <span>{body}</span>
      </div>
      <div className="trd-gate__actions">{action}</div>
    </div>
  )
}
