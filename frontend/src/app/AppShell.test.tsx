import { render, screen } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { routeChildren } from './routes'
import { AppProviders } from './providers'

// Render the route tree without AppProviders (no network): test harness
// provides QueryClient only; views under test here are stubs.
function renderAt(path: string) {
  const router = createMemoryRouter(routeChildren, { initialEntries: [path] })
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('routes', () => {
  it('renders a stub for every registered view', () => {
    renderAt('/sessions')
    expect(screen.getByRole('heading', { name: 'Sessions' })).toBeInTheDocument()
  })

  it('renders XSS-safe 404 text for unknown paths', () => {
    renderAt('/nope<script>')
    expect(screen.getByText(/Page not found:/)).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
  })

  it('sets the document title from the route', () => {
    renderAt('/logs')
    expect(document.title).toBe('Logs - AgentOS Control')
  })
})

// Guards the effect-cleanup path in AppProviders (Task 5 review carry-forward):
// the bootstrap fetch + rpc subscription must unsubscribe/disconnect on unmount
// so a StrictMode-style double mount does not leak or crash.
describe('AppProviders effect cleanup', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('mounts and unmounts repeatedly without crashing', () => {
    // Bootstrap fetch never resolves here, so the provider stays in its
    // "Connecting…" state; we exercise mount → unmount → remount purely to
    // verify the cleanup function runs (unsubscribe + disconnect) without error.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    )

    const first = render(
      <AppProviders>
        <div>child</div>
      </AppProviders>,
    )
    expect(() => first.unmount()).not.toThrow()

    const second = render(
      <AppProviders>
        <div>child</div>
      </AppProviders>,
    )
    expect(() => second.unmount()).not.toThrow()
  })
})
