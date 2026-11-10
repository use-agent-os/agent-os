import { render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { createMemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PREFERENCES_KEY } from '@/stores/preferences'
import { WebApp } from './WebApp'

// The real providers fetch a bootstrap and open a socket; this test is only
// about what WebApp itself does at boot.
vi.mock('@/app/providers', () => ({
  AppProviders: ({ children }: { children: ReactNode }) => <>{children}</>,
}))

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-text-size')
})

describe('WebApp boot', () => {
  it('applies the saved text size', () => {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ textSize: 'large' }))
    const router = createMemoryRouter([{ path: '/', element: <p>ok</p> }])
    render(<WebApp router={router} />)
    expect(document.documentElement.getAttribute('data-text-size')).toBe('large')
  })
})
