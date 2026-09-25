import {
  ArrowLeftRight,
  Lock,
  Rocket,
  SendHorizontal,
  Wallet as WalletIcon,
  Wrench,
  Zap,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { t, type MessageKey } from '~/i18n'
import { formatUsd, walletLabel } from '../logic'
import { ProviderMark, providerMark } from '../ProviderMark'
import { ProviderMenu } from '../ProviderMenu'
import {
  providerLabel,
  type Limits,
  type ProviderId,
  type ProviderStatus,
  type Wallet,
} from '../types'
import type { MissionKind } from './desk-logic'

/**
 * A one-shot swap is not a mission — it creates no job and turns up in no
 * mission list — so it stays a plain quick action. DCA, dip and rebalance
 * left this row when the mission catalogue took them over: two ways to start
 * the same thing, one of them with fewer defaults, is not a shortcut.
 */
const QUICK: readonly { kind: MissionKind; icon: LucideIcon; key: MessageKey }[] = [
  { kind: 'swap', icon: Zap, key: 'trading.quick.swap' },
]

/**
 * The row above the capsule. The permission seat is display-only on
 * purpose: the chat names the authority it has and can never widen it —
 * that happens in Settings. Quick actions fade while typing so the capsule
 * never reflows under the caret.
 */
export function ComposerSeats({
  limits,
  provider,
  providers,
  switching,
  wallet,
  typing,
  onOpenSettings,
  onOpenWallets,
  onSwitchProvider,
  onStartMission,
  onQuick,
  onSend,
  onOpenTools,
}: {
  limits: Limits | null
  provider: ProviderId
  /** Per-provider facts from trading.status; the menu explains each choice. */
  providers?: readonly ProviderStatus[]
  switching?: boolean
  wallet: Wallet | null
  typing: boolean
  onOpenSettings: () => void
  onOpenWallets: () => void
  /** Switching the swap route is allowed from the desk; limits are not. */
  onSwitchProvider?: (id: ProviderId) => void
  /** Opens the mission contract; rendered as the first chip so it lines up with the capsule. */
  onStartMission?: () => void
  onQuick: (kind: MissionKind) => void
  /** Opens the Send sheet: one or many recipients, asked of the desk or sent now. */
  onSend?: () => void
  /** Opens the BOOK's Tools tab: every tool of the desk, on one page. */
  onOpenTools?: () => void
}) {
  return (
    <div className="trd-seats" data-typing={typing || undefined} data-testid="composer-seats">
      <button
        type="button"
        className="trd-seat app-no-drag"
        onClick={onOpenSettings}
        title={t('trading.seat.permission.title')}
        aria-label={t('trading.seat.permission.title')}
        data-testid="permission-seat"
      >
        <Lock className="size-3" strokeWidth={2} aria-hidden />
        <span className="trd-seat__text">
          {limits
            ? `${t('trading.seat.asksAbove')} ${formatUsd(limits.thresholdUsd)} · ${formatUsd(
                limits.dailyCapUsd,
              )}${t('trading.seat.perDay')}`
            : t('trading.seat.permission.unknown')}
        </span>
      </button>
      <ProviderSeat
        provider={provider}
        providers={providers ?? []}
        switching={Boolean(switching)}
        onSwitch={onSwitchProvider}
        onOpenSettings={onOpenSettings}
      />
      <button
        type="button"
        className="trd-seat app-no-drag"
        onClick={onOpenWallets}
        title={t('trading.seat.wallet.title')}
        /* The shrink chain drops `.trd-seat__text` on a narrow pane, which would
           otherwise take this button's whole accessible name with it. */
        aria-label={`${t('trading.seat.wallet.title')}: ${
          wallet ? walletLabel(wallet) : t('trading.seat.noWallet')
        }`}
        data-testid="wallet-seat"
      >
        <WalletIcon className="size-3" strokeWidth={2} aria-hidden />
        <span className="trd-seat__text">
          {wallet ? walletLabel(wallet) : t('trading.seat.noWallet')}
        </span>
      </button>
      <span className="trd-seats__spacer" />
      <div className="trd-quick" role="group" aria-label={t('trading.quick.title')}>
        {onStartMission ? (
          <button
            type="button"
            className="trd-quick__chip trd-quick__chip--mission app-no-drag"
            onClick={onStartMission}
            data-testid="mission-start"
            tabIndex={typing ? -1 : 0}
            aria-label={t('trading.mission.start')}
          >
            <Rocket className="size-3" strokeWidth={2} aria-hidden />
            <span className="trd-quick__chip-text">{t('trading.mission.start')}</span>
          </button>
        ) : null}
        {QUICK.map(({ kind, icon: Icon, key }) => (
          <button
            key={kind}
            type="button"
            className="trd-quick__chip app-no-drag"
            onClick={() => onQuick(kind)}
            data-testid={`quick-${kind}`}
            tabIndex={typing ? -1 : 0}
            aria-label={t(key)}
          >
            <Icon className="size-3" strokeWidth={2} aria-hidden />
            <span className="trd-quick__chip-text">{t(key)}</span>
          </button>
        ))}
        {onSend ? (
          <button
            type="button"
            className="trd-quick__chip app-no-drag"
            onClick={onSend}
            data-testid="quick-send"
            tabIndex={typing ? -1 : 0}
            aria-label={t('trading.quick.send')}
          >
            <SendHorizontal className="size-3" strokeWidth={2} aria-hidden />
            <span className="trd-quick__chip-text">{t('trading.quick.send')}</span>
          </button>
        ) : null}
        {onOpenTools ? (
          <button
            type="button"
            className="trd-quick__chip app-no-drag"
            onClick={onOpenTools}
            title={t('trading.tools.title')}
            data-testid="quick-tools"
            tabIndex={typing ? -1 : 0}
            aria-label={t('trading.tools.chip')}
          >
            <Wrench className="size-3" strokeWidth={2} aria-hidden />
            <span className="trd-quick__chip-text">{t('trading.tools.chip')}</span>
          </button>
        ) : null}
      </div>
    </div>
  )
}

/**
 * Which aggregator routes the swaps, changeable right here (it is not a
 * limit). The desk head's venue pill is the same control in another face.
 */
function ProviderSeat({
  provider,
  providers,
  switching,
  onSwitch,
  onOpenSettings,
}: {
  provider: ProviderId
  providers: readonly ProviderStatus[]
  switching: boolean
  onSwitch?: (id: ProviderId) => void
  onOpenSettings: () => void
}) {
  return (
    <div className="trd-seat__anchor">
      <ProviderMenu
        className="trd-seat app-no-drag"
        testId="provider-seat"
        provider={provider}
        providers={providers}
        switching={switching}
        onSwitch={onSwitch}
        onOpenSettings={onOpenSettings}
      >
        {providerMark(provider) ? (
          <ProviderMark id={provider} size={13} />
        ) : (
          <ArrowLeftRight className="size-3" strokeWidth={2} aria-hidden />
        )}
        <span className="trd-seat__text">{providerLabel(provider)}</span>
      </ProviderMenu>
    </div>
  )
}
