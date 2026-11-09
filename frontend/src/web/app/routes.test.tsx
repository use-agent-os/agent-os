import '@/test/web'
import { screen, waitFor } from '@testing-library/react'
import { Outlet } from 'react-router'
import { describe, expect, it } from 'vitest'
import { FakeRpc } from '@/test/fake-rpc'
import { renderRoutes } from '@/test/render-web'
import { webRouteChildren } from './routes'

const tree = [{ path: '/', element: <Outlet />, children: webRouteChildren }]

function open(path: string, rpc?: FakeRpc) {
  return renderRoutes(tree, { initialEntries: [path], rpc })
}

describe('web routes', () => {
  it('opens /chat from the index', async () => {
    const { router } = open('/')
    await waitFor(() => expect(router.state.location.pathname).toBe('/chat'))
  })

  it.each(['/home', '/sessions', '/projects', '/projects/p1', '/usage', '/automations', '/inbox'])(
    'renders %s as a deep link without redirecting',
    async (path) => {
      const { router } = open(path)
      await new Promise((r) => setTimeout(r, 20))
      expect(screen.queryByTestId('not-found-path')).not.toBeInTheDocument()
      expect(router.state.location.pathname).toBe(path)
    },
  )

  it.each([
    ['/overview', '/home'],
    ['/cron', '/automations'],
    ['/projects?project=P', '/projects/P'],
    ['/health', '/settings/health'],
    ['/channels', '/settings/channels'],
    ['/mcp', '/settings/mcp'],
    ['/skills', '/settings/skills'],
    ['/memory', '/settings/memory'],
    ['/agents', '/settings/agents'],
    ['/env', '/settings/environment'],
    ['/logs', '/settings/logs'],
    ['/approvals', '/settings/approvals'],
    ['/config', '/settings/advanced'],
    ['/setup', '/settings/setup'],
    ['/channels?view=setup', '/settings/channels?setup=1'],
    ['/settings?step=channels', '/settings/channels?setup=1'],
  ])('redirects legacy %s to %s with history replacement', async (from, to) => {
    const { router } = renderRoutes(tree, { initialEntries: ['/usage', from] })
    await waitFor(() =>
      expect(`${router.state.location.pathname}${router.state.location.search}`).toBe(to),
    )
    // Back skips the legacy URL entirely.
    await router.navigate(-1)
    await waitFor(() => expect(router.state.location.pathname).toBe('/usage'))
  })

  it('renders an unknown path as literal text and links Home and Chat', async () => {
    const { container } = open('/<img src=x onerror=alert(1)>')
    const text = await screen.findByTestId('not-found-path')
    expect(text.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByRole('link', { name: 'Go to Home' })).toHaveAttribute('href', '/home')
    expect(screen.getByRole('link', { name: 'Open Chat' })).toHaveAttribute('href', '/chat')
  })

  it('finishes MCP OAuth at the callback path, then shows Settings › MCP', async () => {
    const rpc = new FakeRpc().handle('mcp.oauth.complete', { ok: true })
    const { router } = open('/mcp/oauth/callback?code=abc&state=xyz', rpc)
    await waitFor(() => expect(router.state.location.pathname).toBe('/settings/mcp'))
    expect(rpc.callsTo('mcp.oauth.complete')).toEqual([{ code: 'abc', state: 'xyz' }])
    expect(router.state.location.state).toEqual({ mcpOAuth: { ok: true } })
  })

  it('reports an OAuth callback without code or state', async () => {
    const rpc = new FakeRpc()
    open('/mcp/oauth/callback?error=access_denied&error_description=User%20said%20no', rpc)
    expect(await screen.findByText('MCP authorization failed')).toBeInTheDocument()
    expect(screen.getByText('User said no')).toBeInTheDocument()
    expect(rpc.calls).toEqual([])
  })

  it('reports a failed OAuth completion in place', async () => {
    const rpc = new FakeRpc().handle('mcp.oauth.complete', () => {
      throw new Error('state expired')
    })
    open('/mcp/oauth/callback?code=a&state=b', rpc)
    expect(await screen.findByText('state expired')).toBeInTheDocument()
  })
})
