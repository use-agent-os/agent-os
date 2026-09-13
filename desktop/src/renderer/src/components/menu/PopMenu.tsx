import './menu.css'
import { Check, ChevronRight, type LucideIcon } from 'lucide-react'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

/** Gap kept between a floating menu and the window edge. */
const EDGE = 8

interface MenuContextValue {
  /** Close the whole menu tree (an item was chosen, Escape, click outside). */
  close: () => void
  openSub: string | null
  setOpenSub: (id: string | null) => void
}

const MenuContext = createContext<MenuContextValue | null>(null)

function useMenuContext(): MenuContextValue {
  const ctx = useContext(MenuContext)
  if (!ctx) throw new Error('Menu items must be rendered inside a menu')
  return ctx
}

/** Where a floating menu goes: at a point (context menu) or under a control. */
export type MenuPlace =
  { at: { x: number; y: number } } | { anchor: DOMRect; align?: 'start' | 'end' }

type Origin = 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right'

/**
 * Fit a menu of `size` into the viewport for `place`. A context menu opens
 * down-right of the point and flips when it would run off an edge; an
 * anchored menu hangs under its control, right edges aligned, and flips
 * above when there is no room below.
 */
export function placeMenu(
  place: MenuPlace,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
): { left: number; top: number; origin: Origin } {
  const maxLeft = Math.max(EDGE, viewport.width - size.width - EDGE)
  const maxTop = Math.max(EDGE, viewport.height - size.height - EDGE)
  let left: number
  let top: number
  let flipX = false
  let flipY = false
  if ('at' in place) {
    left = place.at.x
    top = place.at.y
    if (left + size.width > viewport.width - EDGE) {
      left = place.at.x - size.width
      flipX = true
    }
    if (top + size.height > viewport.height - EDGE) {
      top = place.at.y - size.height
      flipY = true
    }
  } else {
    const align = place.align ?? 'end'
    left = align === 'end' ? place.anchor.right - size.width : place.anchor.left
    top = place.anchor.bottom + 4
    if (top + size.height > viewport.height - EDGE && place.anchor.top - size.height - 4 >= EDGE) {
      top = place.anchor.top - size.height - 4
      flipY = true
    }
    flipX = align === 'end'
  }
  left = Math.min(maxLeft, Math.max(EDGE, left))
  top = Math.min(maxTop, Math.max(EDGE, top))
  const origin: Origin = `${flipY ? 'bottom' : 'top'}-${flipX ? 'right' : 'left'}`
  return { left, top, origin }
}

function menuItems(menu: HTMLElement): HTMLElement[] {
  // Only this menu's own rows: a submenu's items belong to the submenu.
  return Array.from(menu.querySelectorAll<HTMLButtonElement>('[role^="menuitem"]')).filter(
    (el) => !el.disabled && el.closest('[role="menu"]') === menu,
  )
}

/**
 * Keyboard and dismissal shared by every menu root: Escape closes (a
 * submenu first), arrows move within the menu that has focus, Right opens
 * a submenu, Left leaves one, a press outside closes, and so does Tab,
 * a scroll or the window losing focus (a fixed panel would drift).
 */
function useMenuRoot(
  ref: React.RefObject<HTMLDivElement | null>,
  onClose: () => void,
  opts: { outsideRef?: React.RefObject<HTMLElement | null>; closeOnScroll: boolean },
) {
  const [openSub, setOpenSub] = useState<string | null>(null)
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])

  // Focus lands on the first row when the menu opens, not on every submenu change.
  useEffect(() => {
    const root = ref.current
    if (root) menuItems(root)[0]?.focus()
  }, [ref])

  useEffect(() => {
    const root = ref.current
    if (!root) return

    const onKey = (e: KeyboardEvent) => {
      const active = document.activeElement as HTMLElement | null
      const inside = Boolean(active && root.contains(active))
      if (e.key === 'Escape') {
        e.stopPropagation()
        e.preventDefault()
        if (openSub) {
          const trigger = root.querySelector<HTMLElement>(`[data-sub-id="${openSub}"] > button`)
          setOpenSub(null)
          trigger?.focus()
        } else {
          closeRef.current()
        }
        return
      }
      if (e.key === 'Tab') {
        closeRef.current()
        return
      }
      const menu = (inside && active?.closest<HTMLElement>('[role="menu"]')) || root
      const items = menuItems(menu)
      if (items.length === 0) return
      const i = active ? items.indexOf(active) : -1
      const focusAt = (n: number) => items[(n + items.length) % items.length]?.focus()
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault()
          focusAt(i + 1)
          break
        case 'ArrowUp':
          e.preventDefault()
          focusAt(i - 1)
          break
        case 'Home':
          e.preventDefault()
          focusAt(0)
          break
        case 'End':
          e.preventDefault()
          focusAt(items.length - 1)
          break
        case 'ArrowRight': {
          const sub = active?.closest<HTMLElement>('.mac-menu__sub')
          if (active?.getAttribute('aria-haspopup') === 'menu' && sub?.dataset.subId) {
            e.preventDefault()
            setOpenSub(sub.dataset.subId)
          }
          break
        }
        case 'ArrowLeft': {
          if (menu !== root) {
            e.preventDefault()
            const trigger = menu.parentElement?.querySelector<HTMLElement>(':scope > button')
            setOpenSub(null)
            trigger?.focus()
          }
          break
        }
      }
    }
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (root.contains(target)) return
      if (opts.outsideRef?.current?.contains(target)) return
      closeRef.current()
    }
    const onScroll = (e: Event) => {
      if (opts.closeOnScroll && !root.contains(e.target as Node)) closeRef.current()
    }
    const onBlur = () => closeRef.current()
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('scroll', onScroll, true)
    window.addEventListener('blur', onBlur)
    window.addEventListener('resize', onBlur)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('resize', onBlur)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- rebinding per submenu state is intended
  }, [ref, openSub])

  // Moving the pointer onto a row outside the open submenu closes it, the
  // way NSMenu does, after a short grace so a diagonal move to the panel
  // survives.
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!openSub) return
      const sub = (e.target as HTMLElement).closest<HTMLElement>('.mac-menu__sub')
      const insideOpen =
        sub?.dataset.subId === openSub || Boolean(sub?.closest(`[data-sub-id="${openSub}"]`))
      if (insideOpen) {
        if (leaveTimer.current) clearTimeout(leaveTimer.current)
        leaveTimer.current = null
        return
      }
      if (leaveTimer.current) return
      leaveTimer.current = setTimeout(() => {
        leaveTimer.current = null
        setOpenSub(null)
      }, 160)
    },
    [openSub],
  )
  useEffect(
    () => () => {
      if (leaveTimer.current) clearTimeout(leaveTimer.current)
    },
    [],
  )

  const ctx = useMemo<MenuContextValue>(
    () => ({ close: () => closeRef.current(), openSub, setOpenSub }),
    [openSub],
  )
  return { ctx, onPointerMove }
}

/**
 * A menu floating over the window: a context menu at the pointer, or the
 * menu of a control that sits inside a scrolling list (portalled so the
 * list cannot clip it). Positions itself once measured and flips at the
 * window edges.
 */
export function PopMenu({
  place,
  onClose,
  label,
  children,
  /** Presses on this element do not count as "outside" (the trigger). */
  triggerRef,
}: {
  place: MenuPlace
  onClose: () => void
  label?: string
  children: ReactNode
  triggerRef?: React.RefObject<HTMLElement | null>
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number; origin: Origin } | null>(null)
  const { ctx, onPointerMove } = useMenuRoot(ref, onClose, {
    outsideRef: triggerRef,
    closeOnScroll: true,
  })

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    setPos(
      placeMenu(
        place,
        { width: rect.width, height: rect.height },
        { width: window.innerWidth, height: window.innerHeight },
      ),
    )
  }, [place])

  const style: CSSProperties = pos
    ? { left: pos.left, top: pos.top }
    : { left: 0, top: 0, visibility: 'hidden' }

  return createPortal(
    <MenuContext.Provider value={ctx}>
      <div
        ref={ref}
        className="mac-menu app-no-drag"
        data-placement="float"
        data-origin={pos?.origin ?? 'top-left'}
        role="menu"
        aria-label={label}
        style={style}
        onPointerMove={onPointerMove}
        onContextMenu={(e) => e.preventDefault()}
      >
        {children}
      </div>
    </MenuContext.Provider>,
    document.body,
  )
}

/**
 * A menu hung under its trigger, inside the trigger's positioned parent.
 * For headers and chips that are not inside a scroller.
 */
export function Menu({
  onClose,
  label,
  align = 'end',
  children,
}: {
  onClose: () => void
  label?: string
  align?: 'start' | 'end'
  children: ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)
  const parentRef = useRef<HTMLElement | null>(null)
  useLayoutEffect(() => {
    parentRef.current = ref.current?.parentElement ?? null
  }, [])
  const { ctx, onPointerMove } = useMenuRoot(ref, onClose, {
    outsideRef: parentRef,
    closeOnScroll: false,
  })
  return (
    <MenuContext.Provider value={ctx}>
      <div
        ref={ref}
        className="mac-menu app-no-drag"
        data-placement="anchor"
        data-align={align}
        role="menu"
        aria-label={label}
        onPointerMove={onPointerMove}
      >
        {children}
      </div>
    </MenuContext.Provider>
  )
}

/** One row. `checked` turns the leading slot into a checkmark. */
export function MenuItem({
  icon: Icon,
  label,
  onSelect,
  tone,
  disabled,
  checked,
  aside,
  role = checked === undefined ? 'menuitem' : 'menuitemcheckbox',
}: {
  icon?: LucideIcon
  label: string
  onSelect: () => void
  tone?: 'danger'
  disabled?: boolean
  checked?: boolean
  /** Trailing text: a shortcut, a count. */
  aside?: ReactNode
  role?: 'menuitem' | 'menuitemcheckbox' | 'menuitemradio'
}) {
  const ctx = useMenuContext()
  return (
    <button
      type="button"
      role={role}
      aria-checked={checked === undefined ? undefined : checked}
      className="mac-menu__item"
      data-tone={tone}
      disabled={disabled}
      onClick={() => {
        ctx.close()
        onSelect()
      }}
    >
      {checked !== undefined ? (
        <span className="mac-menu__check" aria-hidden>
          {checked ? <Check className="size-3.5" strokeWidth={2.5} /> : null}
        </span>
      ) : Icon ? (
        <Icon className="size-3.5" strokeWidth={1.75} aria-hidden />
      ) : (
        <span className="mac-menu__check" aria-hidden />
      )}
      <span className="mac-menu__label">{label}</span>
      {aside ? <span className="mac-menu__aside">{aside}</span> : null}
    </button>
  )
}

/**
 * A row that opens a panel to its side. Opens on hover (after a beat),
 * click, Right or Return; the current value can show beside the chevron.
 */
export function MenuSub({
  icon: Icon,
  label,
  value,
  children,
}: {
  icon?: LucideIcon
  label: string
  value?: string
  children: ReactNode
}) {
  const ctx = useMenuContext()
  const id = useId()
  const open = ctx.openSub === id
  const wrapRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [side, setSide] = useState<'right' | 'left'>('right')
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [focusFirst, setFocusFirst] = useState(false)

  useLayoutEffect(() => {
    if (!open) return
    const panel = panelRef.current
    if (!panel) return
    const rect = panel.getBoundingClientRect()
    setSide(rect.right > window.innerWidth - EDGE ? 'left' : 'right')
    if (focusFirst) {
      menuItems(panel)[0]?.focus()
      setFocusFirst(false)
    }
  }, [open, focusFirst])

  useEffect(
    () => () => {
      if (hoverTimer.current) clearTimeout(hoverTimer.current)
    },
    [],
  )

  const openNow = (withFocus: boolean) => {
    setFocusFirst(withFocus)
    ctx.setOpenSub(id)
  }

  return (
    <div ref={wrapRef} className="mac-menu__sub" data-sub-id={id} data-side={side}>
      <button
        type="button"
        role="menuitem"
        aria-haspopup="menu"
        aria-expanded={open}
        className="mac-menu__item"
        data-active={open}
        onPointerEnter={() => {
          if (open) return
          hoverTimer.current = setTimeout(() => openNow(false), 110)
        }}
        onPointerLeave={() => {
          if (hoverTimer.current) clearTimeout(hoverTimer.current)
          hoverTimer.current = null
        }}
        onClick={() => (open ? ctx.setOpenSub(null) : openNow(true))}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            openNow(true)
          }
        }}
      >
        {Icon ? (
          <Icon className="size-3.5" strokeWidth={1.75} aria-hidden />
        ) : (
          <span className="mac-menu__check" aria-hidden />
        )}
        <span className="mac-menu__label">{label}</span>
        <span className="mac-menu__aside">
          {value ? <span>{value}</span> : null}
          <ChevronRight className="size-3" strokeWidth={2} aria-hidden />
        </span>
      </button>
      {open ? (
        <div ref={panelRef} className="mac-menu" role="menu" aria-label={label}>
          {children}
        </div>
      ) : null}
    </div>
  )
}

export function MenuSep() {
  return <div className="mac-menu__sep" role="separator" />
}

export function MenuHeading({ children }: { children: ReactNode }) {
  return <div className="mac-menu__heading">{children}</div>
}

export function MenuNote({ children }: { children: ReactNode }) {
  return <p className="mac-menu__note">{children}</p>
}
