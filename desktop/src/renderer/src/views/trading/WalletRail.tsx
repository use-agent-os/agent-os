import { Download, Lock, MoreHorizontal, Plus, Star } from 'lucide-react'
import { PopMenu } from '~/components/menu/PopMenu'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { cn } from '~/lib/utils'
import { blockieDataUrl } from './blockie'
import {
  capUsage,
  formatPct,
  formatUsd,
  pnlTone,
  sameAddress,
  shortAddress,
  walletLabel,
} from './logic'
import { Money } from './parts'
import type { ChainStatus, Limits, Totals, Wallet } from './types'
import { usePopUp, WalletMenuItems } from './WalletMenu'

export type WalletSelection = 'all' | string

export type WalletAction =
  | { kind: 'create' }
  | { kind: 'import' }
  | { kind: 'rename'; wallet: Wallet }
  | { kind: 'export'; wallet: Wallet }
  | { kind: 'remove'; wallet: Wallet }
  | { kind: 'receive'; wallet: Wallet }
  | { kind: 'lock' }

/**
 * The left column: every wallet with its total, the primary starred, "all"
 * on top, and under the list the agent's budget for today as a meter.
 * Selection is view state for the ledger and the ticket.
 */
export function WalletRail({
  wallets,
  totals,
  selected,
  onSelect,
  onAction,
  onSetPrimary,
  limits,
  limitsWallet,
  chains,
  manualUnlock,
}: {
  wallets: Wallet[]
  /** Per-wallet totals from the portfolio, keyed by address (lowercase). */
  totals: Map<string, Totals>
  selected: WalletSelection
  onSelect: (selection: WalletSelection) => void
  onAction: (action: WalletAction) => void
  onSetPrimary: (wallet: Wallet) => void
  limits: Limits | undefined
  /** Whose budget the meter shows. */
  limitsWallet: Wallet | null
  chains: ChainStatus[]
  /** Manual unlock mode: the rail offers to lock the vault. */
  manualUnlock: boolean
}) {
  const all = [...totals.values()].reduce(
    (sum, tt) => ({
      valueUsd: sum.valueUsd + tt.valueUsd,
      change24hUsd: (sum.change24hUsd ?? 0) + (tt.change24hUsd ?? 0),
    }),
    { valueUsd: 0, change24hUsd: 0 as number | null },
  )
  // The per-wallet rows show their day as a percentage; the "all" row must
  // read in the same unit, so its dollar move is taken against yesterday's
  // total rather than shown as a figure nothing beside it uses.
  const allBase = all.valueUsd - (all.change24hUsd ?? 0)
  const allPct =
    allBase > 0 && all.change24hUsd !== null ? (all.change24hUsd / allBase) * 100 : null

  return (
    <aside className="trd-rail" aria-label={t('trading.rail.title')}>
      <div className="trd-rail__head">
        <span className="trd-rail__title">{t('trading.rail.title')}</span>
        {manualUnlock ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t('trading.rail.lock')}
            title={t('trading.rail.lock')}
            onClick={() => onAction({ kind: 'lock' })}
          >
            <Lock className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          </Button>
        ) : null}
      </div>
      <div className="trd-rail__list" role="listbox" aria-label={t('trading.rail.title')}>
        {wallets.length > 1 ? (
          <button
            type="button"
            role="option"
            aria-selected={selected === 'all'}
            className="trd-wallet trd-wallet--all app-no-drag"
            onClick={() => onSelect('all')}
          >
            <span className="trd-wallet__name">
              <span>{t('trading.rail.all')}</span>
            </span>
            <span className="trd-wallet__addr">{wallets.length}</span>
            <span className="trd-wallet__value">
              <Money value={all.valueUsd} compact />
              <span
                className="trd-wallet__delta trd-num"
                data-tone={pnlTone(allPct)}
                title={formatUsd(all.change24hUsd, { signed: true })}
              >
                {formatPct(allPct, { signed: true })}
              </span>
            </span>
          </button>
        ) : null}
        {wallets.map((w) => (
          <WalletRow
            key={w.address}
            wallet={w}
            totals={totals.get(w.address.toLowerCase())}
            selected={selected !== 'all' && sameAddress(selected, w.address)}
            onSelect={() => onSelect(w.address)}
            onAction={onAction}
            onSetPrimary={() => onSetPrimary(w)}
            chains={chains}
          />
        ))}
        <button
          type="button"
          className="trd-rail__add app-no-drag"
          onClick={() => onAction({ kind: 'create' })}
        >
          <Plus className="size-3.5" strokeWidth={2} aria-hidden />
          {t('trading.rail.create')}
        </button>
        <button
          type="button"
          className="trd-rail__add app-no-drag"
          onClick={() => onAction({ kind: 'import' })}
        >
          <Download className="size-3.5" strokeWidth={2} aria-hidden />
          {t('trading.rail.import')}
        </button>
      </div>

      {limits && limitsWallet ? <Budget limits={limits} wallet={limitsWallet} /> : null}
    </aside>
  )
}

/**
 * What the agent may still spend today, as a meter. It belongs beside the
 * wallets, but it is a guardrail rather than a list item: when the rail is
 * away it lays itself out as a strip over the ledger instead of vanishing.
 */
export function Budget({
  limits,
  wallet,
  strip,
}: {
  limits: Limits
  wallet: Wallet
  strip?: boolean
}) {
  const usage = capUsage(limits.spentTodayUsd, limits.dailyCapUsd)
  return (
    <div className={cn('trd-budget', strip && 'trd-budget--strip')} data-testid="trading-budget">
      <div className="trd-budget__label">
        <span>{t('trading.rail.limits')}</span>
        <span className="trd-mono">{walletLabel(wallet)}</span>
      </div>
      <div
        className="trd-budget__meter"
        data-full={usage.fraction >= 1 ? 'true' : undefined}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={limits.dailyCapUsd}
        aria-valuenow={limits.spentTodayUsd}
        aria-label={t('trading.rail.limits')}
      >
        <span style={{ width: `${Math.round(usage.fraction * 100)}%` }} />
      </div>
      <div className="trd-budget__text">
        <b>{formatUsd(usage.leftUsd)}</b> {t('trading.rail.limits.left')}{' '}
        <b>{formatUsd(limits.dailyCapUsd)}</b>
        {/* The approval threshold is the half this tile can afford to drop when
            the rail turns into a strip: the composer's permission seat states
            it too. The remainder of the cap does not appear anywhere else. */}
        <span className="trd-budget__threshold">
          {' · '}
          {t('trading.rail.limits.threshold')} <b>{formatUsd(limits.thresholdUsd)}</b>
        </span>
      </div>
    </div>
  )
}

function WalletRow({
  wallet,
  totals,
  selected,
  onSelect,
  onAction,
  onSetPrimary,
  chains,
}: {
  wallet: Wallet
  totals: Totals | undefined
  selected: boolean
  onSelect: () => void
  onAction: (action: WalletAction) => void
  onSetPrimary: () => void
  chains: ChainStatus[]
}) {
  const { place, triggerRef, close, toggle } = usePopUp('end')
  const label = walletLabel(wallet)
  const delta = totals?.change24hUsd ?? null
  const pct = totals?.change24hPct ?? null

  return (
    <div
      role="option"
      aria-selected={selected}
      tabIndex={0}
      className={cn('trd-wallet app-no-drag')}
      data-testid="wallet-row"
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect()
        }
      }}
    >
      <img className="trd-wallet__mark" src={blockieDataUrl(wallet.address, 20)} alt="" />
      <span className="trd-wallet__name">
        <span>{label}</span>
        {wallet.primary ? (
          <Star
            className="trd-wallet__star size-3"
            strokeWidth={2}
            fill="currentColor"
            aria-label={t('trading.rail.primary')}
          />
        ) : null}
      </span>
      <span className="trd-wallet__addr">{shortAddress(wallet.address)}</span>
      <span className="trd-wallet__value">
        {totals ? (
          <Money value={totals.valueUsd} compact />
        ) : (
          <span className="trd-skel" style={{ width: 48 }} />
        )}
        <span className="trd-wallet__delta trd-num" data-tone={pnlTone(delta)}>
          {totals ? formatPct(pct, { signed: true }) : ''}
        </span>
      </span>
      <span className="trd-wallet__menu">
        <Button
          ref={triggerRef}
          variant="ghost"
          size="icon"
          aria-label={t('trading.rail.menu')}
          aria-haspopup="menu"
          aria-expanded={place !== null}
          onClick={(e) => {
            e.stopPropagation()
            toggle(e)
          }}
        >
          <MoreHorizontal className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
        </Button>
        {place ? (
          <PopMenu
            place={place}
            triggerRef={triggerRef}
            onClose={close}
            label={t('trading.rail.menu')}
          >
            <WalletMenuItems
              wallet={wallet}
              chains={chains}
              onAction={onAction}
              onSetPrimary={onSetPrimary}
            />
          </PopMenu>
        ) : null}
      </span>
    </div>
  )
}
