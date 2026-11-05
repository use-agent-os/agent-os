import { Copy, ExternalLink, KeyRound, Pencil, QrCode, Star, Trash2 } from 'lucide-react'
import { useCallback, useRef, useState, type MouseEvent } from 'react'
import { toast } from 'sonner'
import { MenuItem, type MenuPlace } from '~/components/menu/PopMenu'
import { t } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import type { ChainStatus, Wallet } from './types'
import type { WalletAction } from './WalletRail'

/**
 * A pop-up button's state, for a PopMenu. The desk's wallet menus all sit in
 * something that clips: the head in .trd-hero, whose overflow: hidden keeps
 * the corner light inside the card, and the rail's rows in their scrolling
 * list. Hung from inside either, a menu loses its lower rows, so they float
 * over the window instead.
 */
export function usePopUp(align: 'start' | 'end') {
  // The anchor rect is captured on open, so no ref is read during render.
  const [place, setPlace] = useState<MenuPlace | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const close = useCallback(() => {
    // The menu is at the end of the document now, so Escape, Tab and a pick
    // must bring focus back or the keyboard starts over from the top.
    const fromMenu = Boolean(document.activeElement?.closest('[role="menu"]'))
    setPlace(null)
    if (fromMenu) triggerRef.current?.focus({ preventScroll: true })
  }, [])
  const toggle = (e: MouseEvent<HTMLButtonElement>) => {
    const anchor = e.currentTarget.getBoundingClientRect()
    setPlace((p) => (p ? null : { anchor, align }))
  }
  return { place, triggerRef, close, toggle }
}

/**
 * Everything you can do to one wallet, as menu rows.
 *
 * Two places open this menu — the rail's row and the head's ⋯ — and they must
 * offer the same things in the same order, or the desk has two answers to
 * "where do I rename a wallet". One list, two triggers.
 */
export function WalletMenuItems({
  wallet,
  chains,
  onAction,
  onSetPrimary,
}: {
  wallet: Wallet
  chains: ChainStatus[]
  onAction: (action: WalletAction) => void
  onSetPrimary: () => void
}) {
  return (
    <>
      <MenuItem
        icon={Copy}
        label={t('trading.rail.receive')}
        onSelect={() => void copyAddress(wallet.address)}
      />
      <MenuItem
        icon={QrCode}
        label={t('trading.rail.qr')}
        onSelect={() => onAction({ kind: 'receive', wallet })}
      />
      {chains.map((c) => (
        <MenuItem
          key={c.chainId}
          icon={ExternalLink}
          label={`${t('trading.rail.explorer')} · ${c.name}`}
          onSelect={() =>
            void desktopApi().app.openExternal(`${c.explorer}/address/${wallet.address}`)
          }
        />
      ))}
      {!wallet.primary ? (
        <MenuItem icon={Star} label={t('trading.rail.setPrimary')} onSelect={onSetPrimary} />
      ) : null}
      <MenuItem
        icon={Pencil}
        label={t('trading.rail.rename')}
        onSelect={() => onAction({ kind: 'rename', wallet })}
      />
      <MenuItem
        icon={KeyRound}
        label={t('trading.rail.export')}
        onSelect={() => onAction({ kind: 'export', wallet })}
      />
      <MenuItem
        icon={Trash2}
        tone="danger"
        label={t('trading.rail.remove')}
        onSelect={() => onAction({ kind: 'remove', wallet })}
      />
    </>
  )
}

/** Put an address on the clipboard and say so. Silent where there is no clipboard. */
export async function copyAddress(address: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(address)
    toast.success(t('trading.rail.copied'), { id: 'trd-copy' })
  } catch {
    /* clipboard unavailable */
  }
}
