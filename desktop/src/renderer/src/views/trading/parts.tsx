import { Copy, LoaderCircle, TriangleAlert, X } from 'lucide-react'
import {
  Component,
  useEffect,
  useId,
  useRef,
  useState,
  type ErrorInfo,
  type ReactNode,
} from 'react'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { BALANCE_MASK, useBalancePrivacy } from '~/stores/balance-privacy'
import { ChainMark } from './ChainMark'
import {
  chainName,
  clampSymbol,
  errorText,
  formatUsd,
  formatUsdCell,
  orderTone,
  pnlTone,
  shortAddress,
  type PnlTone,
} from './logic'
import type { OrderStatus, Token } from './types'

/** The desk's small vocabulary: a sheet, a status pill, a token cell, a figure that ticks. */

/**
 * A token symbol in a row: clamped to 12 characters (the whole in `title`)
 * and bidi-isolated, so an on-chain name cannot widen the row or reorder
 * the amount beside it with a right-to-left override.
 */
export function Sym({ symbol, className }: { symbol: string; className?: string }) {
  return (
    <span className={cn('trd-sym', className)} title={symbol} data-testid="sym">
      {clampSymbol(symbol)}
    </span>
  )
}

export function Sheet({
  title,
  onClose,
  role = 'dialog',
  wide,
  widest,
  children,
  foot,
  note,
}: {
  title: string
  onClose: () => void
  role?: 'dialog' | 'alertdialog'
  wide?: boolean
  /** Wider still, for a sheet laying cards out in a grid rather than a column. */
  widest?: boolean
  children: ReactNode
  foot?: ReactNode
  note?: ReactNode
}) {
  const titleId = useId()
  return (
    <ModalShell
      role={role}
      labelledBy={titleId}
      onClose={onClose}
      overlayClassName="trd-modal__overlay"
      className={cn('trd-modal', wide && 'trd-modal--wide', widest && 'trd-modal--widest')}
    >
      <header className="trd-modal__head">
        <h2 id={titleId}>{title}</h2>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t('trading.sheet.cancel')}
          onClick={onClose}
        >
          <X className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
        </Button>
      </header>
      <div className="trd-modal__body">{children}</div>
      {foot ? (
        <footer className="trd-modal__foot">
          {note ? <span className="trd-modal__note">{note}</span> : null}
          {foot}
        </footer>
      ) : null}
    </ModalShell>
  )
}

export function StatusPill({ status }: { status: OrderStatus }) {
  return (
    <span className="trd-status" data-tone={orderTone(status)} data-status={status}>
      {t(`trading.orders.status.${status}`)}
    </span>
  )
}

export function TokenLogo({ token, size = 22 }: { token: Token; size?: number }) {
  const [broken, setBroken] = useState(false)
  // Nothing indexes every token: with no art and no ticker, the address's own
  // first characters are still a stable, distinguishable mark.
  const initials = (token.symbol || token.address.slice(2, 5)).slice(0, 3).toUpperCase()
  return (
    <span className="trd-asset__logo" style={{ width: size, height: size }} aria-hidden>
      {token.logoUrl && !broken ? (
        <img
          src={token.logoUrl}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setBroken(true)}
        />
      ) : (
        initials
      )}
    </span>
  )
}

export function AssetCell({
  token,
  showChain,
  sub,
  tag,
  wallet,
  onCopy,
}: {
  token: Token
  showChain?: boolean
  sub?: string
  /** A one-word state ahead of the name, e.g. "junk" for a row shown on request. */
  tag?: string
  /** Whose row this is, when the table mixes wallets: the same token twice
   *  otherwise reads as a duplicate. */
  wallet?: string
  /** Put the token's contract address on the clipboard. A small icon after
   *  the ticker; the click stays out of the row it sits in. */
  onCopy?: () => void
}) {
  // Not everything is indexed, and a row with two blanks in it reads as a bug.
  // Fall back to what is always true: the address, and that it has no name.
  const ticker = token.symbol || shortAddress(token.address)
  const name = sub ?? token.name ?? ''
  return (
    <span className="trd-asset">
      <TokenLogo token={token} />
      <span className="trd-asset__text">
        <span className="trd-asset__symbol">
          <span className="trd-asset__ticker" title={token.symbol || token.address}>
            {ticker}
          </span>
          {showChain ? (
            // The mark alone: the network's name would push the ticker's
            // neighbours to a second line. It stays on hover and for readers.
            <span className="trd-asset__chainmark" title={chainName(token.chainId)}>
              <ChainMark chainId={token.chainId} />
            </span>
          ) : null}
          {!token.verified && !token.native ? (
            <TriangleAlert
              className="trd-asset__warn size-3"
              strokeWidth={2}
              aria-label={t('trading.holdings.unverified')}
            />
          ) : null}
          {onCopy ? (
            <button
              type="button"
              className="trd-asset__copy app-no-drag"
              aria-label={`${t('trading.holdings.copyAddress')} ${ticker}`}
              title={`${t('trading.holdings.copyAddress')}: ${token.address}`}
              data-testid="asset-copy"
              onClick={(e) => {
                e.stopPropagation()
                onCopy()
              }}
            >
              <Copy className="size-3" strokeWidth={1.75} aria-hidden />
            </button>
          ) : null}
        </span>
        <span className="trd-asset__name">
          {tag ? <span className="trd-asset__tag">{tag}</span> : null}
          {wallet ? (
            <span className="trd-asset__wallet trd-mono" data-testid="asset-wallet">
              {wallet}
            </span>
          ) : null}
          {name || t('trading.token.unknown')}
        </span>
      </span>
    </span>
  )
}

/**
 * A number that flashes when it changes. The wash is the only motion the
 * desk uses for data, so an eye on the table sees what moved.
 */
export function Tick({
  value,
  children,
  tone,
  className,
}: {
  value: string | number | null
  children: ReactNode
  tone?: PnlTone
  className?: string
}) {
  const prev = useRef(value)
  const [flash, setFlash] = useState(0)
  useEffect(() => {
    if (prev.current !== value && prev.current !== null) setFlash((n) => n + 1)
    prev.current = value
  }, [value])
  return (
    <span
      key={flash}
      className={cn('trd-num', flash > 0 && 'trd-tick', className)}
      data-tone={tone}
    >
      {children}
    </span>
  )
}

export function Money({
  value,
  signed,
  toned,
  compact,
  /** Collapse anything under a cent to "<$0.01" so a tile keeps its width;
   *  the caller is expected to carry the exact figure in a `title`. */
  cell,
  /** Show the figure even while balances are hidden: a sheet about to remove
   *  a wallet states what it holds, and that is not the place for a mask. */
  plain,
}: {
  value: number | null | undefined
  signed?: boolean
  toned?: boolean
  compact?: boolean
  cell?: boolean
  plain?: boolean
}) {
  const hidden = useBalancePrivacy((s) => s.hidden) && !plain
  if (hidden) {
    // No tone and no tick: the colour says which way the figure went and the
    // flash says when it moved, and both are the figure leaking.
    return (
      <span className="trd-num" data-masked="true">
        {BALANCE_MASK}
      </span>
    )
  }
  return (
    <Tick value={value ?? null} tone={toned ? pnlTone(value) : undefined}>
      {cell ? formatUsdCell(value, { signed }) : formatUsd(value, { signed, compact })}
    </Tick>
  )
}

/* Entrance: the hero value counts from 0 to its figure over the window the
   choreography reserves for it (see --enter-count-* in desk/desk.css), then
   hands back to the live, ticking value. Shared by the BOOK's hero and the
   full desk's, so both count on the same clock.
   The frames are written straight into the node's text rather than through
   state: a setState per frame re-rendered the whole panel forty times in the
   busiest 420 ms of the switch, which is main-thread work the landing panels
   were competing with. React sees two renders now — start and end. */
const COUNT_DELAY_MS = 220
const COUNT_MS = 420

export function useCountUp(
  target: number,
  active: boolean,
): { counting: boolean; attach: (el: HTMLSpanElement | null) => void } {
  // The node arrives through state rather than a ref so the frame loop can
  // start the moment it mounts, and so nothing ref-shaped crosses render.
  const [node, attach] = useState<HTMLSpanElement | null>(null)
  const [counting, setCounting] = useState(false)
  const spent = useRef(false)
  // Hidden balances do not count up: the frames are written straight into
  // the node, past anything that could mask them.
  const hidden = useBalancePrivacy((s) => s.hidden)

  useEffect(() => {
    if (!active || hidden) {
      spent.current = false
      setCounting(false)
      return
    }
    if (!spent.current) setCounting(true)
  }, [active, hidden])

  useEffect(() => {
    if (!counting || !node) return
    const start = performance.now() + COUNT_DELAY_MS
    let raf = requestAnimationFrame(function tick(now: number) {
      const p = Math.min(1, Math.max(0, (now - start) / COUNT_MS))
      node.textContent = formatUsd(target * (1 - Math.pow(1 - p, 3)))
      if (p < 1) {
        raf = requestAnimationFrame(tick)
      } else {
        spent.current = true
        setCounting(false)
      }
    })
    return () => cancelAnimationFrame(raf)
  }, [counting, node, target])

  return { counting, attach }
}

export function Spinner({ className }: { className?: string }) {
  return <LoaderCircle className={cn('trd-spin size-3.5', className)} strokeWidth={2} aria-hidden />
}

export function Skeleton({ width }: { width: number | string }) {
  return <span className="trd-skel" style={{ width }} aria-hidden />
}

export function Empty({
  icon,
  title,
  body,
  action,
  tone,
}: {
  icon: ReactNode
  title: string
  body: string
  action?: ReactNode
  tone?: 'error'
}) {
  return (
    <div className="trd-empty" data-tone={tone}>
      {icon}
      <b>{title}</b>
      <p>{body}</p>
      {action ? <div className="mt-2 flex gap-2">{action}</div> : null}
    </div>
  )
}

/**
 * A read that failed is not an empty list: it says what the RPC said, and
 * offers the one thing that helps. Every tab and sheet uses this rather than
 * its own empty state, so a dead gateway never reads as "nothing here".
 */
/**
 * A tab that throws while rendering is contained here: it says so, names the
 * error and offers a retry, and the rest of the window stays up. Key it by
 * the tab so moving to another tab starts clean.
 */
export class PanelBoundary extends Component<
  { children: ReactNode; name?: string },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: unknown): { error: Error } {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[trading] panel ${this.props.name ?? ''} failed to render`, error, info)
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div role="alert" data-testid="panel-error">
        <Empty
          tone="error"
          icon={<TriangleAlert className="size-8" strokeWidth={1.25} aria-hidden />}
          title={t('trading.panel.failed')}
          body={error.message || String(error)}
          action={
            <Button
              variant="primary"
              onClick={() => this.setState({ error: null })}
              data-testid="panel-error-retry"
            >
              {t('trading.error.retry')}
            </Button>
          }
        />
      </div>
    )
  }
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <Empty
      tone="error"
      icon={<TriangleAlert className="size-8" strokeWidth={1.25} aria-hidden />}
      title={t('trading.error.title')}
      body={errorText(error)}
      action={
        <Button variant="primary" onClick={onRetry} data-testid="trading-error-retry">
          {t('trading.error.retry')}
        </Button>
      }
    />
  )
}
