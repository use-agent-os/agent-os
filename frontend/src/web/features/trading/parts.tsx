import { ExternalLink, LoaderCircle, TriangleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { t, type MessageKey } from '@/i18n'
import '@/i18n/en/webTrading'
import { cn } from '@/lib/utils'
import { chainName, chainShort, formatUsd, pnlTone } from '@/views/trading/logic'
import type {
  ChainRead,
  OrderStatus,
  TriggerStatus,
  MandateStatus,
  LpStatus,
} from '@/views/trading/types'
import { Badge } from '@/web/components/ui/badge'
import { BALANCE_MASK, clampSym, clampText, useBalancePrivacy } from './safety'

/**
 * A USD figure, masked when amounts are hidden. `toned` colours it by sign
 * (the sign is in the text too, never in the colour alone).
 */
export function Money({
  value,
  signed,
  toned,
  className,
}: {
  value: number | null | undefined
  signed?: boolean
  toned?: boolean
  className?: string
}) {
  const hidden = useBalancePrivacy((s) => s.hidden)
  if (hidden)
    return (
      <span className={cn('trd-num', className)} data-masked>
        {BALANCE_MASK}
      </span>
    )
  return (
    <span className={cn('trd-num', className)} data-tone={toned ? pnlTone(value) : undefined}>
      {formatUsd(value, { signed })}
    </span>
  )
}

/** A token amount (already formatted from its decimal string), masked when hidden. */
export function Amount({ text, className }: { text: string; className?: string }) {
  const hidden = useBalancePrivacy((s) => s.hidden)
  return (
    <span className={cn('trd-num', className)} data-masked={hidden || undefined}>
      {hidden ? BALANCE_MASK : text}
    </span>
  )
}

/**
 * An engine note or failure reason under a row. It is free text and often
 * names amounts and balances ("holds 0 USDC, needs 149.98"), so it masks
 * whole with the amounts; clamped plain text otherwise.
 */
export function EngineNote({ text, max = 120 }: { text: string | null | undefined; max?: number }) {
  const hidden = useBalancePrivacy((s) => s.hidden)
  if (!text) return null
  return (
    <p className="trd-order__note" data-masked={hidden || undefined}>
      {hidden ? BALANCE_MASK : clampText(text, max)}
    </p>
  )
}

/**
 * A token's mark: its initials in a round tile. Logos come from third
 * parties, so the web page never loads them.
 */
export function TokenMark({ symbol, address }: { symbol: string; address: string }) {
  const initials = (symbol || address.slice(2, 5)).replace(/[^\p{L}\p{N}]/gu, '').slice(0, 3)
  return (
    <span className="trd-token-mark" aria-hidden>
      {initials.toUpperCase() || '?'}
    </span>
  )
}

/** Symbol, name and network of a token, as text only. */
export function TokenCell({
  symbol,
  name,
  address,
  chainId,
  verified = true,
  showChain = true,
}: {
  symbol: string
  name?: string | null
  address: string
  chainId: number
  verified?: boolean
  showChain?: boolean
}) {
  const shown = clampSym(symbol || address.slice(0, 8))
  return (
    <span className="trd-token">
      <TokenMark symbol={symbol} address={address} />
      <span className="trd-token__text">
        <span className="trd-token__symbol">
          <span className="truncate">{shown}</span>
          {!verified ? (
            <TriangleAlert
              className="trd-token__warn"
              aria-label={t('webTrading.unverified')}
              role="img"
            />
          ) : null}
        </span>
        <span className="trd-token__sub">
          {showChain ? <ChainChip chainId={chainId} /> : null}
          {name ? <span className="truncate">{clampText(name, 28)}</span> : null}
        </span>
      </span>
    </span>
  )
}

export function ChainChip({ chainId }: { chainId: number }) {
  return (
    <span className="trd-chain" data-chain={chainId} title={chainName(chainId)}>
      {chainShort(chainId)}
    </span>
  )
}

/**
 * An outbound explorer link: built by `safety.ts`, never from a payload URL.
 * Its accessible name starts with the visible text (the short address or
 * hash) and adds `label` after it, so a voice user can say what they see
 * (WCAG 2.5.3); only an icon-only link takes `label` as its whole name.
 */
export function ExplorerLink({
  href,
  label,
  children,
}: {
  href: string | null
  label: string
  children?: ReactNode
}) {
  if (!href) return children ? <>{children}</> : null
  return (
    <a
      className="trd-link"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={children ? undefined : label}
      title={label}
    >
      {children}
      {children ? <span className="sr-only">, {label}</span> : null}
      <ExternalLink aria-hidden className="size-3.5 shrink-0" />
    </a>
  )
}

/**
 * "Syncing" beside the totals while a pass someone waits on runs (the shared
 * `syncShown`, as the desktop's hero shows it). A status, not an alert: it
 * is read when reached, never interrupts.
 */
export function SyncNote({ syncing }: { syncing: boolean }) {
  if (!syncing) return null
  return (
    <p className="trd-sync" role="status">
      <LoaderCircle aria-hidden className="trd-sync__spin size-3.5 shrink-0" />
      {t('webTrading.syncing')}
    </p>
  )
}

const REASON_MAX = 160

/**
 * A warning that some chain reads of the shown wallets failed or stopped
 * part way, so the figures may be short. It names networks only, never an
 * amount, so it stays with the amounts hidden. The engine's reasons are
 * untrusted text (often an RPC URL): clamped, plain, never a link. A note,
 * not a live region, so a poll does not announce it again.
 */
export function ChainReadWarning({ problems }: { problems: readonly ChainRead[] }) {
  if (problems.length === 0) return null
  const chains = [...new Set(problems.map((p) => p.chainId))].map(chainName)
  const reasons = [
    ...new Set(
      problems.map((p) => {
        const reason = clampText(p.reason, REASON_MAX)
        const chain = chainName(p.chainId)
        return reason
          ? t('webTrading.readReason', { chain, reason })
          : t('webTrading.readReasonNone', { chain })
      }),
    ),
  ]
  return (
    <div className="trd-callout trd-read-warn" role="note" data-testid="trd-read-warning">
      <p className="trd-read-warn__head">
        <TriangleAlert aria-hidden className="size-4 shrink-0" />
        {t('webTrading.readIncomplete', { chains: chains.join(', ') })}
      </p>
      <details className="trd-read-warn__details">
        <summary>{t('webTrading.readReasons')}</summary>
        <ul>
          {reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </details>
    </div>
  )
}

type Tone = 'neutral' | 'accent' | 'ok' | 'warn' | 'danger' | 'info'

const STATUS: Record<string, { key: MessageKey; tone: Tone }> = {
  quoted: { key: 'webTrading.statusQuoted', tone: 'neutral' },
  awaiting_approval: { key: 'webTrading.statusAwaiting', tone: 'warn' },
  approved: { key: 'webTrading.statusApproved', tone: 'info' },
  rejected: { key: 'webTrading.statusRejected', tone: 'neutral' },
  expired: { key: 'webTrading.statusExpired', tone: 'neutral' },
  submitted: { key: 'webTrading.statusSubmitted', tone: 'info' },
  confirmed: { key: 'webTrading.statusConfirmed', tone: 'ok' },
  failed: { key: 'webTrading.statusFailed', tone: 'danger' },
  active: { key: 'webTrading.statusActive', tone: 'ok' },
  paused: { key: 'webTrading.statusPaused', tone: 'warn' },
  completed: { key: 'webTrading.statusCompleted', tone: 'neutral' },
  stopped: { key: 'webTrading.statusStopped', tone: 'neutral' },
  armed: { key: 'webTrading.statusArmed', tone: 'accent' },
  triggered: { key: 'webTrading.statusTriggered', tone: 'info' },
  done: { key: 'webTrading.statusDone', tone: 'ok' },
  'in-range': { key: 'webTrading.lpInRange', tone: 'ok' },
  'above-range': { key: 'webTrading.lpAbove', tone: 'warn' },
  'below-range': { key: 'webTrading.lpBelow', tone: 'warn' },
  'full-range': { key: 'webTrading.lpFull', tone: 'info' },
  closed: { key: 'webTrading.lpClosed', tone: 'neutral' },
}

export function StatusBadge({
  status,
}: {
  status: OrderStatus | MandateStatus | TriggerStatus | LpStatus
}) {
  const known = STATUS[status]
  return (
    <Badge tone={known?.tone ?? 'neutral'} dot>
      {known ? t(known.key) : clampText(status, 20)}
    </Badge>
  )
}

/** Who started it: the person, an agent, or a transfer from outside. */
export function initiatorLabel(initiator: string): string {
  if (initiator === 'agent') return t('webTrading.byAgent')
  if (initiator === 'external') return t('webTrading.byExternal')
  return t('webTrading.byYou')
}

const WHEN = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

/** A ledger time (ms, or seconds from older rows) as a local date and time. */
export function formatWhen(ts: number | string | null | undefined): string {
  if (ts === null || ts === undefined || ts === '') return '—'
  const n = typeof ts === 'string' ? Date.parse(ts) : ts < 1e12 ? ts * 1000 : ts
  return Number.isFinite(n) ? WHEN.format(n) : '—'
}

const KIND: Record<string, MessageKey> = {
  swap: 'webTrading.kindSwap',
  send: 'webTrading.kindSend',
  revoke: 'webTrading.kindRevoke',
  bridge: 'webTrading.kindBridge',
  lp_add: 'webTrading.kindLpAdd',
  lp_collect: 'webTrading.kindLpCollect',
  lp_remove: 'webTrading.kindLpRemove',
  deposit: 'webTrading.kindDeposit',
  withdraw: 'webTrading.kindWithdraw',
  approval: 'webTrading.kindApproval',
  gas: 'webTrading.kindGas',
  unwrap: 'webTrading.kindUnwrap',
}

/** An order or ledger entry kind as a word; unknown kinds as their (clamped) id. */
export function kindLabel(kind: string | null | undefined): string {
  const key = KIND[kind ?? 'swap']
  return key ? t(key) : clampText(kind, 16)
}
