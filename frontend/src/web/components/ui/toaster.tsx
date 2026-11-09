import { Toaster as Sonner } from 'sonner'
import { useTheme } from '@/stores/theme'

/**
 * Brief confirmations only (design-system spec "Toast discipline"): polite,
 * top-centre so they never cover the composer, themed with the console.
 */
export function Toaster() {
  const theme = useTheme((s) => s.mode)
  return (
    <Sonner
      theme={theme}
      position="top-center"
      closeButton
      toastOptions={{
        classNames: {
          toast: 'border border-border bg-surface-2 text-text shadow-2',
          description: 'text-text-muted',
        },
      }}
    />
  )
}
