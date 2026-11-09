import { lazy, Suspense } from 'react'
import type { ShortcutHelpRenderer } from '@/components/KeyboardShortcuts'

const ShortcutOverview = lazy(async () => ({
  default: (await import('./ShortcutOverview')).ShortcutOverview,
}))

/** Renders the web overview for the kept shortcut provider (loaded on first open). */
export const ShortcutOverviewHost: ShortcutHelpRenderer = ({ open, onClose }) =>
  open ? (
    <Suspense fallback={null}>
      <ShortcutOverview open={open} onClose={onClose} />
    </Suspense>
  ) : null
