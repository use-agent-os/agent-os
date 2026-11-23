import { ExternalLink, TriangleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { t, type MessageKey } from '@/i18n'
import '@/i18n/en/webTrading'
import { cn } from '@/lib/utils'
import { chainName, chainShort, clampSymbol, formatUsd, pnlTone } from '@/views/trading/logic'
import type { OrderStatus, TriggerStatus, MandateStatus, LpStatus } from '@/views/trading/types'
import { Badge } from '@/web/components/ui/badge'
import { BALANCE_MASK, clampText, useBalancePrivacy } from './safety'

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
  const shown = clampSymbol(symbol || address.slice(0, 8))
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

/** An outbound explorer link: built by `safety.ts`, never from a payload URL. */
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
      aria-label={label}
      title={label}
    >
      {children}
      <ExternalLink aria-hidden className="size-3.5 shrink-0" />
    </a>
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
