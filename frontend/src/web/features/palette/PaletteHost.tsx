import { lazy, Suspense, useState } from 'react'
import { useShell } from '@/web/shell/shell-store'
import { WebShortcuts } from './WebShortcuts'

// cmdk and the palette load in their own chunk on first open, then stay
// mounted so later opens are instant.
const CommandPalette = lazy(async () => ({
  default: (await import('./CommandPalette')).CommandPalette,
}))

export function PaletteHost() {
  const open = useShell((s) => s.paletteOpen)
  const setOpen = useShell((s) => s.setPaletteOpen)
  const [loaded, setLoaded] = useState(false)
  if (open && !loaded) setLoaded(true)
  return (
    <>
      <WebShortcuts />
      {loaded ? (
        <Suspense fallback={null}>
          <CommandPalette open={open} onOpenChange={setOpen} />
        </Suspense>
      ) : null}
    </>
  )
}
