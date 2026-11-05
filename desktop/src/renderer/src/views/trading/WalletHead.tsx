import {
  ChevronDown,
  Copy,
  Download,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  QrCode,
  Settings2,
  Star,
} from 'lucide-react'
import { MenuItem, MenuSep, PopMenu } from '~/components/menu/PopMenu'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { blockieDataUrl } from './blockie'
import { copyAddress, usePopUp, WalletMenuItems } from './WalletMenu'
import { formatUsd, sameAddress, shortAddress, walletLabel } from './logic'
import type { ChainStatus, Totals, Wallet } from './types'
import type { WalletAction, WalletSelection } from './WalletRail'

/**
 * Who you are looking at, above what it is worth.
 *
 * The desk used to answer those two questions in two places — the rail said
 * which wallet, the head said the figure — so switching wallets moved a number
 * with nothing beside it to say whose number it now was. This is one line:
 * the mark, the address, the wallet's name under it, and the actions that act
 * on *this* wallet. The figure below it is then unambiguous.
 *
 * The same head serves the full desk and the BOOK beside the chat: one place
 * to learn, and a wallet is named the same way in both. `compact` is the only
 * difference — the BOOK is 300 px wide and cannot spend 40 px on a mark.
 */
export function WalletHead({
  wallets,
  selected,
  onSelect,
  totals,
  onAction,
  onSetPrimary,
  onManage,
  chains,
  railOpen,
  onToggleRail,
  compact,
}: {
  wallets: Wallet[]
  selected: WalletSelection
  onSelect: (selection: WalletSelection) => void
  /** Per-wallet totals, keyed by lowercased address, for the switcher rows. */
  totals: Map<string, Totals>
  onAction: (action: WalletAction) => void
  onSetPrimary: (wallet: Wallet) => void
  /** Opens the wallet manager, where the BOOK sends anything list-shaped. */
  onManage?: () => void
  chains: ChainStatus[]
  /** The rail is showing. Omitted where there is no rail to toggle. */
  railOpen?: boolean
  onToggleRail?: () => void
  /** Beside the chat rather than over the desk: smaller mark, smaller line. */
  compact?: boolean
}) {
  const {
    place: switcherPlace,
    triggerRef: switcherRef,
    close: closeSwitcher,
    toggle: toggleSwitcher,
  } = usePopUp('start')
  const {
    place: menuPlace,
    triggerRef: menuRef,
    close: closeMenu,
    toggle: toggleMenu,
  } = usePopUp('end')
  const wallet =
    selected === 'all' ? null : (wallets.find((w) => sameAddress(w.address, selected)) ?? null)
  const many = wallets.length > 1

  const allUsd = [...totals.values()].reduce((sum, tt) => sum + tt.valueUsd, 0)

  const markSize = compact ? 30 : 40

  return (
    <header className="trd-head" data-compact={compact ? 'true' : undefined}>
      <div className="trd-head__who">
        <button
          ref={switcherRef}
          type="button"
          className="trd-head__id app-no-drag"
          aria-haspopup="menu"
          aria-expanded={switcherPlace !== null}
          aria-label={t('trading.head.switch')}
          data-testid="wallet-switcher"
          // Even with one wallet the menu is where Create, Import and Manage
          // live, so it always opens; only the "all" row needs a second wallet.
          onClick={toggleSwitcher}
        >
          {wallet ? (
            <img className="trd-head__mark" src={blockieDataUrl(wallet.address, markSize)} alt="" />
          ) : (
            <span className="trd-head__stack" aria-hidden>
              {wallets.slice(0, 3).map((w) => (
                <img key={w.address} src={blockieDataUrl(w.address, markSize - 16)} alt="" />
              ))}
            </span>
          )}
          <span className="trd-head__text">
            <span className="trd-head__line">
              <b className="trd-mono" data-testid="wallet-head-address">
                {wallet ? shortAddress(wallet.address) : t('trading.rail.all')}
              </b>
              {wallet?.primary ? (
                <Star
                  className="trd-head__star size-3"
                  strokeWidth={2}
                  fill="currentColor"
                  aria-label={t('trading.rail.primary')}
                />
              ) : null}
              <ChevronDown className="size-3.5" strokeWidth={2} aria-hidden />
            </span>
            <span className="trd-head__sub">
              {wallet ? walletLabel(wallet) : `${wallets.length} ${t('trading.head.wallets')}`}
            </span>
          </span>
        </button>

        {switcherPlace ? (
          <PopMenu
            place={switcherPlace}
            triggerRef={switcherRef}
            onClose={closeSwitcher}
            label={t('trading.head.switch')}
            className="trd-head__switcher"
          >
            {many ? (
              <>
                <MenuItem
                  role="menuitemradio"
                  checked={selected === 'all'}
                  label={t('trading.rail.all')}
                  aside={<span className="trd-mono">{formatUsd(allUsd, { compact: true })}</span>}
                  onSelect={() => onSelect('all')}
                />
                <MenuSep />
              </>
            ) : null}
            {/* The wallets scroll; the "all" row and the commands stay in reach. */}
            <div className="trd-head__wallets">
              {wallets.map((w) => {
                const tt = totals.get(w.address.toLowerCase())
                return (
                  <MenuItem
                    key={w.address}
                    role="menuitemradio"
                    checked={selected !== 'all' && sameAddress(selected, w.address)}
                    label={walletLabel(w)}
                    aside={
                      <span className="trd-mono">
                        {tt ? formatUsd(tt.valueUsd, { compact: true }) : ''}
                      </span>
                    }
                    onSelect={() => onSelect(w.address)}
                  />
                )
              })}
            </div>
            <MenuSep />
            <MenuItem
              icon={Plus}
              label={t('trading.rail.create')}
              onSelect={() => onAction({ kind: 'create' })}
            />
            <MenuItem
              icon={Download}
              label={t('trading.rail.import')}
              onSelect={() => onAction({ kind: 'import' })}
            />
            {onManage ? (
              <MenuItem icon={Settings2} label={t('trading.rail.manage')} onSelect={onManage} />
            ) : null}
          </PopMenu>
        ) : null}
      </div>

      <div className="trd-head__acts">
        {wallet ? (
          <>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('trading.rail.receive')}
              title={t('trading.rail.receive')}
              onClick={() => void copyAddress(wallet.address)}
            >
              <Copy className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('trading.rail.qr')}
              title={t('trading.rail.qr')}
              onClick={() => onAction({ kind: 'receive', wallet })}
            >
              <QrCode className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            </Button>
          </>
        ) : null}
        {many && onToggleRail ? (
          <Button
            variant="ghost"
            size="icon"
            aria-label={railOpen ? t('trading.head.hideList') : t('trading.head.showList')}
            title={railOpen ? t('trading.head.hideList') : t('trading.head.showList')}
            data-testid="rail-toggle"
            onClick={onToggleRail}
          >
            {railOpen ? (
              <PanelLeftClose
                className="size-4 text-muted-foreground"
                strokeWidth={1.75}
                aria-hidden
              />
            ) : (
              <PanelLeftOpen
                className="size-4 text-muted-foreground"
                strokeWidth={1.75}
                aria-hidden
              />
            )}
          </Button>
        ) : null}
        {wallet ? (
          <>
            <Button
              ref={menuRef}
              variant="ghost"
              size="icon"
              aria-label={t('trading.rail.menu')}
              aria-haspopup="menu"
              aria-expanded={menuPlace !== null}
              onClick={toggleMenu}
            >
              <MoreHorizontal
                className="size-4 text-muted-foreground"
                strokeWidth={1.75}
                aria-hidden
              />
            </Button>
            {menuPlace ? (
              <PopMenu
                place={menuPlace}
                triggerRef={menuRef}
                onClose={closeMenu}
                label={t('trading.rail.menu')}
              >
                <WalletMenuItems
                  wallet={wallet}
                  chains={chains}
                  onAction={onAction}
                  onSetPrimary={() => onSetPrimary(wallet)}
                />
              </PopMenu>
            ) : null}
          </>
        ) : null}
      </div>
    </header>
  )
}
