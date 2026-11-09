import { t } from '@/i18n'
import '@/i18n/en/webShell'
import { Sheet, SheetContent, SheetTitle } from '@/web/components/ui/sheet'
import { ListPane } from './ListPane'
import { Rail } from './Rail'
import { focusMainTarget } from './edge-swipe'
import { useShell } from './shell-store'

/** The phone navigation drawer (shell spec "Mobile navigation drawer"). */
export function MobileDrawer() {
  const open = useShell((s) => s.drawerOpen)
  const setOpen = useShell((s) => s.setDrawerOpen)
  const onNavigate = () => {
    setOpen(false)
    focusMainTarget()
  }
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent
        side="left"
        aria-describedby={undefined}
        className="w-[min(92vw,380px)] flex-row"
        onOpenAutoFocus={(e) => {
          // Land on the current destination (or the drawer itself), not on a
          // rail button whose focus tooltip would swallow the first Escape.
          e.preventDefault()
          const content = e.currentTarget as HTMLElement
          const current = content.querySelector<HTMLElement>(
            '[data-testid="list-pane"] [aria-current="page"]',
          )
          ;(current ?? content).focus({ preventScroll: true })
        }}
      >
        <SheetTitle className="sr-only">{t('webShell.drawerTitle')}</SheetTitle>
        <Rail onNavigate={onNavigate} />
        <div className="flex min-w-0 flex-1">
          <ListPane onNavigate={onNavigate} resizable={false} />
        </div>
      </SheetContent>
    </Sheet>
  )
}
