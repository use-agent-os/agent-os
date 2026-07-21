import { createContext, useContext, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

type ShellSlotTarget = HTMLElement | null | undefined

const ShellHeaderTargetContext = createContext<ShellSlotTarget>(undefined)
const ShellSidebarActionTargetContext = createContext<
  { target: ShellSlotTarget; onAction: () => void } | undefined
>(undefined)

interface ShellHeaderSlotProviderProps {
  children: ReactNode
  target: HTMLElement | null
  sidebarActionTarget: HTMLElement | null
  onSidebarAction: () => void
}

/** Makes the AppShell route-specific slots available to the active view. */
export function ShellHeaderSlotProvider({
  children,
  target,
  sidebarActionTarget,
  onSidebarAction,
}: ShellHeaderSlotProviderProps) {
  return (
    <ShellHeaderTargetContext.Provider value={target}>
      <ShellSidebarActionTargetContext.Provider
        value={{ target: sidebarActionTarget, onAction: onSidebarAction }}
      >
        {children}
      </ShellSidebarActionTargetContext.Provider>
    </ShellHeaderTargetContext.Provider>
  )
}

/**
 * Places view-specific controls in the single AppShell header.
 *
 * A bare view (tests or an isolated story) renders the controls in place. When
 * AppShell is present but its ref has not committed yet, rendering is deferred
 * so the controls never flash as a second stacked bar.
 */
export function ShellHeaderPortal({ children }: { children: ReactNode }) {
  const target = useContext(ShellHeaderTargetContext)
  if (target === undefined) return children
  if (target === null) return null
  return createPortal(children, target)
}

/** Places the active view's primary creation action directly below the sidebar brand. */
export function ShellSidebarActionPortal({ children }: { children: ReactNode }) {
  const slot = useContext(ShellSidebarActionTargetContext)
  if (slot === undefined) return children
  if (slot.target === null || slot.target === undefined) return null
  return createPortal(
    <div className="shell-sidebar__primary-action-content" onClickCapture={slot.onAction}>
      {children}
    </div>,
    slot.target,
  )
}
