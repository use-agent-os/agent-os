import { RouterProvider } from 'react-router'
import { ThemeProvider } from '~/theme/ThemeProvider'
import { GatewayProviders } from './GatewayProviders'
import { router } from './routes'

export function App() {
  return (
    <ThemeProvider>
      <GatewayProviders>
        <RouterProvider router={router} />
      </GatewayProviders>
    </ThemeProvider>
  )
}
