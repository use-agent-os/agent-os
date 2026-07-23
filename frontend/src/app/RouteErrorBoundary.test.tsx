import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { RouteErrorBoundary, routeErrorCopy } from './RouteErrorBoundary'
import { routeChildren } from './routes'

function BrokenView(): never {
  throw new Error('provider lookup failed: development-only detail')
}

function renderBrokenRoute() {
  const router = createMemoryRouter(
    [
      {
        path: '/broken',
        element: <BrokenView />,
        errorElement: <RouteErrorBoundary />,
      },
      { path: '/overview', element: <h1>Overview recovered</h1> },
    ],
    { initialEntries: ['/broken'] },
  )
  return { router, ...render(<RouterProvider router={router} />) }
}

describe('RouteErrorBoundary', () => {
  it('guards every registered child route instead of using React Router fallback UI', () => {
    expect(routeChildren.length).toBeGreaterThan(0)
    expect(routeChildren.every((route) => route.errorElement != null)).toBe(true)
  })

  it('replaces the router developer screen with accessible recovery actions', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    renderBrokenRoute()

    expect(screen.getByRole('heading', { name: 'This view hit a snag' })).toHaveFocus()
    expect(screen.getByRole('alert')).toHaveTextContent('workspace intact')
    expect(screen.getByRole('button', { name: 'Reload view' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to Overview' })).toHaveAttribute(
      'href',
      '/overview',
    )
    expect(screen.queryByText(/react_stack_bottom_frame/i)).not.toBeInTheDocument()
    consoleError.mockRestore()
  })

  it('reloads the current route through the router', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = userEvent.setup()
    const { router } = renderBrokenRoute()
    const navigate = vi.spyOn(router, 'navigate')

    await user.click(screen.getByRole('button', { name: 'Reload view' }))
    expect(navigate).toHaveBeenCalledWith(0)
    consoleError.mockRestore()
  })

  it('can recover by navigating to Overview', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = userEvent.setup()
    renderBrokenRoute()

    await user.click(screen.getByRole('link', { name: 'Go to Overview' }))
    expect(screen.getByRole('heading', { name: 'Overview recovered' })).toBeInTheDocument()
    consoleError.mockRestore()
  })

  it('never exposes an error message or stack in production copy', () => {
    const error = new Error('secret provider token')
    error.stack = 'sensitive stack path'

    const copy = routeErrorCopy(error, false)

    expect(copy.developerMessage).toBeUndefined()
    expect(JSON.stringify(copy)).not.toContain('secret provider token')
    expect(JSON.stringify(copy)).not.toContain('sensitive stack path')
  })
})
