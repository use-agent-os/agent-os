import * as DialogPrimitive from '@radix-ui/react-dialog'
import * as React from 'react'
import { cn } from '@/lib/utils'
import { OverlayLayer } from './overlay-layer'
import { useRestoreFocus } from './restore-focus'

// A side sheet: the mobile navigation drawer and the phone Details / side
// panel sheets. A modal dialog underneath, so focus is trapped, the rest of the
// page is inert, and Escape or a backdrop tap closes it.

export const Sheet = DialogPrimitive.Root
export const SheetTrigger = DialogPrimitive.Trigger
export const SheetClose = DialogPrimitive.Close
export const SheetTitle = DialogPrimitive.Title
export const SheetDescription = DialogPrimitive.Description

export function SheetContent({
  className,
  children,
  side = 'left',
  onOpenAutoFocus,
  onCloseAutoFocus,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & { side?: 'left' | 'right' | 'bottom' }) {
  const focus = useRestoreFocus(onOpenAutoFocus, onCloseAutoFocus)
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay
        data-slot="sheet-overlay"
        className="anim-overlay-in fixed inset-0 z-[var(--z-drawer)] bg-scrim"
      />
      <DialogPrimitive.Content
        data-slot="sheet-content"
        data-side={side}
        className={cn(
          'fixed z-[var(--z-drawer)] flex flex-col bg-surface-1 shadow-3 outline-none',
          side === 'left' &&
            'anim-drawer-in inset-y-0 left-0 w-[min(85vw,320px)] border-r border-border',
          side === 'right' &&
            'inset-y-0 right-0 w-[min(92vw,420px)] border-l border-border anim-overlay-in',
          side === 'bottom' &&
            'inset-x-0 bottom-0 max-h-[85dvh] rounded-t-xl border-t border-border anim-overlay-in',
          'pb-[env(safe-area-inset-bottom)]',
          className,
        )}
        {...props}
        {...focus}
      >
        <OverlayLayer />
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
}
