import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router'
import { AppProviders } from './app/providers'
import { AppShell } from './app/AppShell'
import { routeChildren } from './app/routes'
import './styles/globals.css'

const basename = import.meta.env.BASE_URL.replace(/static\/dist\/?$/, '').replace(/\/$/, '')
const router = createBrowserRouter([{ element: <AppShell />, children: routeChildren }], {
  basename: basename || '/',
})

createRoot(document.getElementById('app')!).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
)
