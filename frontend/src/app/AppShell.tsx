import { NavLink, Outlet } from 'react-router'
import { Moon, Sun } from 'lucide-react'
import { Toaster } from '@/components/ui/sonner'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/stores/theme'
import { useConnection } from '@/stores/connection'
import { VIEWS } from './routes'

export function AppShell() {
  const mode = useTheme((s) => s.mode)
  const toggle = useTheme((s) => s.toggle)
  const connState = useConnection((s) => s.state)

  return (
    <div className="flex h-dvh font-sans">
      <aside className="w-56 shrink-0 border-r p-3">
        <div className="mb-4 px-2 font-semibold">AgentOS Control</div>
        <nav aria-label="Main">
          {VIEWS.map((v) => (
            <NavLink
              key={v.path}
              to={`/${v.path}`}
              className={({ isActive }) =>
                `block rounded px-2 py-1.5 text-sm ${isActive ? 'bg-accent font-medium' : 'text-muted-foreground hover:bg-accent/50'}`
              }
            >
              {v.title}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        {connState !== 'connected' && (
          <div role="status" className="bg-destructive/10 px-4 py-1.5 text-sm">
            {connState === 'connecting' ? 'Connecting to gateway…' : 'Disconnected — reconnecting…'}
          </div>
        )}
        <header className="flex items-center justify-end border-b px-4 py-2">
          <Button
            variant="ghost"
            size="icon"
            onClick={toggle}
            title={`Theme: ${mode}`}
            aria-label={`Theme: ${mode}. Toggle theme`}
            aria-pressed={mode === 'dark'}
          >
            {mode === 'dark' ? <Moon className="size-4" /> : <Sun className="size-4" />}
          </Button>
        </header>
        <main className="min-h-0 flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
      <Toaster />
    </div>
  )
}
