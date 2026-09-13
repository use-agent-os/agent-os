import { QuickActions } from './sidebar/QuickActions'
import { SearchField } from './sidebar/SearchField'
import { SessionList } from './sidebar/SessionList'
import { SidebarFooter } from './sidebar/SidebarFooter'
import { SidebarResizer } from './SidebarResizer'

import { useUi } from '~/stores/ui'

/**
 * Full-height translucent source list. The top strip is left to the traffic
 * lights; everything below scrolls independently of the footer.
 */
export function Sidebar() {
  const open = useUi((s) => s.sidebarOpen)
  const width = useUi((s) => s.sidebarWidth)
  if (!open) return null

  return (
    <aside className="mac-sidebar relative flex shrink-0 flex-col" style={{ width }}>
      <div className="app-drag shrink-0" style={{ height: 'var(--toolbar-height)' }} />
      <QuickActions />
      <div className="py-3">
        <SearchField />
      </div>
      <SessionList />
      <SidebarFooter />
      <SidebarResizer />
    </aside>
  )
}
