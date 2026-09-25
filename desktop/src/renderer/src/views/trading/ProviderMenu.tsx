import { ChevronDown, Settings2 } from 'lucide-react'
import { useCallback, useRef, useState, type ReactNode } from 'react'
import { MenuItem, MenuSep, PopMenu, type MenuPlace } from '~/components/menu/PopMenu'
import { t } from '~/i18n'
import { ProviderMark } from './ProviderMark'
import { PROVIDERS, providerLabel, type ProviderId, type ProviderStatus } from './types'

/**
 * Which aggregator routes the swaps, as a pop-up button.
 *
 * Two places on the desk switch it — the venue pill in the desk head and the
 * route seat under the desk chat's composer — and they must list the same
 * providers, explain them the same way and treat the current one the same,
 * or the desk has two answers to "what does picking Uniswap do". One control,
 * two faces: the caller draws the face, this owns the button, the menu and
 * the switch.
 */
export function ProviderMenu({
  provider,
  providers,
  switching,
  onSwitch,
  onOpenSettings,
  className,
  testId,
  children,
}: {
  provider: ProviderId
  /** Per-provider facts from trading.status; the menu explains each choice. */
  providers: readonly ProviderStatus[]
  /** A switch is on its way to the gateway. */
  switching: boolean
  onSwitch?: (id: ProviderId) => void
  onOpenSettings: () => void
  className: string
  testId: string
  /** The face: the route's mark and name, in the caller's own chip. */
  children: ReactNode
}) {
  // The anchor rect is captured on open, so no ref is read during render.
  const [place, setPlace] = useState<MenuPlace | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const close = useCallback(() => {
    // Escape, Tab and a pick all leave from inside the menu: focus goes back
    // to the button, as with any pop-up button, and Tab moves on from there.
    // A click elsewhere then takes focus wherever it lands.
    const fromMenu = Boolean(document.activeElement?.closest('[role="menu"]'))
    setPlace(null)
    if (fromMenu) triggerRef.current?.focus({ preventScroll: true })
  }, [])
  const facts = (id: ProviderId): string => {
    const row = providers.find((p) => p.id === id)
    if (!row) return ''
    if (row.needsKey) {
      return row.keyConfigured
        ? t('trading.seat.provider.keyOk')
        : t('trading.seat.provider.needsKey')
    }
    return t('trading.seat.provider.noKey')
  }
  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={className}
        onClick={(e) => {
          if (switching) return
          const rect = e.currentTarget.getBoundingClientRect()
          setPlace((p) => (p ? null : { anchor: rect, align: 'start' }))
        }}
        onKeyDown={(e) => {
          // Down or Up opens it too: the menu-button keys, beside Return and Space.
          if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !place && !switching) {
            e.preventDefault()
            setPlace({ anchor: e.currentTarget.getBoundingClientRect(), align: 'start' })
          }
        }}
        title={t('trading.seat.provider.title')}
        // A narrow row can hide the face's name; the button's name keeps the route.
        aria-label={`${t('trading.seat.provider.title')}: ${providerLabel(provider)}`}
        aria-haspopup="menu"
        aria-expanded={place !== null}
        // Not `disabled`: Chromium drops focus from a focused control that
        // becomes disabled, so a switch picked from the keyboard would leave
        // the person nowhere. It waits instead, and keeps its place.
        aria-disabled={switching || undefined}
        data-testid={testId}
      >
        {children}
        <ChevronDown className="size-3 opacity-70" strokeWidth={2} aria-hidden />
      </button>
      {place ? (
        // Anchored, so it flips above the button when the window's foot is near.
        <PopMenu
          place={place}
          triggerRef={triggerRef}
          onClose={close}
          label={t('trading.seat.provider.title')}
        >
          {PROVIDERS.map((p) => (
            <MenuItem
              key={p.id}
              role="menuitemradio"
              checked={p.id === provider}
              mark={<ProviderMark id={p.id} size={13} />}
              label={p.label}
              aside={facts(p.id)}
              onSelect={() => {
                if (p.id !== provider) onSwitch?.(p.id)
              }}
            />
          ))}
          <MenuSep />
          <MenuItem
            icon={Settings2}
            // An empty mark slot: the labels in this menu line up in one column.
            mark={<span aria-hidden />}
            label={t('trading.seat.provider.settings')}
            onSelect={onOpenSettings}
          />
        </PopMenu>
      ) : null}
    </>
  )
}
