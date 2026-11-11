import type { FocusEvent } from 'react'
import { Toaster } from '@/web/components/ui/toaster'

/**
 * The console's toaster as the shell mounts it. Sonner sends focus back to
 * wherever it came from as soon as focus leaves its list. That suits its
 * Alt+T hotkey (which focuses the list itself) but turns sequential Tab into a
 * loop between a toast and the control before it. Focus that arrives on a
 * toast or its buttons by Tab is hidden from sonner, so Tab moves on normally.
 * Placement (below the top bars, centred on the main column) is in shell.css.
 */
export function ShellToaster() {
  const onFocusCapture = (e: FocusEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement
    if (!target.hasAttribute('data-sonner-toaster')) e.stopPropagation()
  }
  return (
    <div className="contents" onFocusCapture={onFocusCapture}>
      <Toaster />
    </div>
  )
}
