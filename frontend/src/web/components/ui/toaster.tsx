import type { CSSProperties } from 'react'
import { Toaster as Sonner } from 'sonner'
import { useTheme } from '@/stores/theme'

// Sonner paints from its own CSS variables, which outrank utility classes on
// the toast: theme it through them so toasts sit on the overlay surface.
const TOAST_VARS = {
  '--normal-bg': 'var(--surface-overlay)',
  '--normal-border': 'var(--border-strong)',
  '--normal-text': 'var(--text)',
  '--border-radius': 'var(--radius-card)',
} as CSSProperties

/**
 * Brief confirmations only (design-system spec "Toast discipline"): polite,
 * bottom-right, themed with the console. The shell lifts them above the
 * composer and the approval tray (shell.css "Floating layers").
 */
export function Toaster() {
  const theme = useTheme((s) => s.mode)
  return (
    <Sonner
      theme={theme}
      position="bottom-right"
      closeButton
      style={TOAST_VARS}
      toastOptions={{
        classNames: {
          toast: 'border border-border bg-surface-overlay text-text shadow-overlay font-sans',
          title: 'font-medium',
          description: 'text-text-muted',
          closeButton:
            'border-border-strong! bg-surface-overlay! text-text-muted! hover:text-text!',
        },
      }}
    />
  )
}
