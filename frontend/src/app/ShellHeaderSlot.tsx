import { createContext, useContext, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

type ShellSlotTarget = HTMLElement | null | undefined

const ShellHeaderTargetContext = createContext<ShellSlotTarget>(undefined)
const ShellPrimaryActionTargetContext = createContext<
  { target: ShellSlotTarget; onAction: () => void } | undefined
>(undefined)

interface ShellHeaderSlotProviderProps {
  children: ReactNode
  target: HTMLElement | null
  primaryActionTarget: HTMLElement | null
  onPrimaryAction: () => void
}

/** Makes the AppShell route-specific Chat-header slots available to the active view. */
export function ShellHeaderSlotProvider({
  children,
  target,
  primaryActionTarget,
  onPrimaryAction,
}: ShellHeaderSlotProviderProps) {
  return (
    <ShellHeaderTargetContext.Provider value={target}>
      <ShellPrimaryActionTargetContext.Provider
        value={{ target: primaryActionTarget, onAction: onPrimaryAction }}
      >
        {children}
      </ShellPrimaryActionTargetContext.Provider>
    </ShellHeaderTargetContext.Provider>
  )
}

/**
 * Places view-specific context controls in the floating Chat header.
 *
 * A bare view (tests or an isolated story) renders the controls in place. When
 * AppShell is present but its ref has not committed yet, rendering is deferred
 * so the controls never flash inside the route body.
 */
export function ShellHeaderPortal({ children }: { children: ReactNode }) {
  const target = useContext(ShellHeaderTargetContext)
  if (target === undefined) return children
  if (target === null) return null
  return createPortal(children, target)
}

/** Places the active view's primary creation action inside the floating Chat header. */
export function ShellPrimaryActionPortal({ children }: { children: ReactNode }) {
  const slot = useContext(ShellPrimaryActionTargetContext)
  if (slot === undefined) return children
  if (slot.target === null || slot.target === undefined) return null
  return createPortal(
    <div className="shell-chat-header__primary-action-content" onClickCapture={slot.onAction}>
      {children}
    </div>,
    slot.target,
  )
}
