import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router'
import { AppProviders } from './app/providers'
import { AppShell } from './app/AppShell'
import { routeChildren } from './app/routes'
import { RouteErrorBoundary } from './app/RouteErrorBoundary'
import { controlBasePath } from './lib/control-base'
import './styles/globals.css'
import './styles/control-surface.css'

const basename = controlBasePath()
const router = createBrowserRouter(
  [
    {
      element: <AppShell />,
      errorElement: <RouteErrorBoundary />,
      children: routeChildren,
    },
  ],
  {
    basename,
  },
)

createRoot(document.getElementById('app')!).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
)
