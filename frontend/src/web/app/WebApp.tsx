import { useEffect } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router'
import { AppProviders } from '@/app/providers'
import { controlBasePath } from '@/lib/control-base'
import { initPreferences } from '@/stores/preferences'
import { TooltipProvider } from '@/web/components/ui/tooltip'
import { ShortcutOverviewHost } from '@/web/features/palette/overview-host'
import { Shell } from '@/web/shell/Shell'
import { RouteErrorBoundary } from './RouteErrorBoundary'
import { webRouteChildren } from './routes'

/** The route tree: the shell layout around every route (D5). */
export const webRoutes = [
  {
    path: '/',
    element: <Shell />,
    errorElement: <RouteErrorBoundary />,
    children: webRouteChildren,
  },
]

export function createWebRouter(basename: string = controlBasePath()) {
  return createBrowserRouter(webRoutes, { basename })
}

/** The rebuilt web console: the kept providers composed around the new router. */
export function WebApp({ router }: { router: ReturnType<typeof createWebRouter> }) {
  // Apply the saved text size (and re-read the other preferences) at boot.
  useEffect(() => initPreferences(), [])
  return (
    <AppProviders shortcutHelp={ShortcutOverviewHost}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </AppProviders>
  )
}
