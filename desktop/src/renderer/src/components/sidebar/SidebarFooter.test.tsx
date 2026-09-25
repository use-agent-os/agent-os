import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { STOPPED_GATEWAY, type GatewayState } from '@shared/gateway'
import { useGateway } from '~/stores/gateway'
import { SidebarFooter } from './SidebarFooter'

const start = vi.fn(async () => {})
const stop = vi.fn(async () => {})

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

function renderFooter(state: GatewayState, at = '/sessions') {
  useGateway.setState({ status: { ...STOPPED_GATEWAY, state }, busy: false, start, stop })
  return render(
    <MemoryRouter initialEntries={[at]}>
      <SidebarFooter />
      <Where />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  start.mockClear()
  stop.mockClear()
})

describe('SidebarFooter', () => {
  it('holds only controls that act: New session, Sync and More did nothing', () => {
    renderFooter('stopped')
    const names = (role: 'link' | 'button') =>
      screen.getAllByRole(role).map((el) => el.getAttribute('aria-label'))
    expect(names('link')).toEqual(['Home'])
    expect(names('button')).toEqual(['Start gateway'])
  })

  it('Home goes back to the keyless home', () => {
    renderFooter('running', '/sessions/agent%3Amain%3Awebchat%3Aabc')
    fireEvent.click(screen.getByRole('link', { name: 'Home' }))
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/sessions$/)
  })

  it('the gateway light starts a stopped gateway and stops a running one', () => {
    const view = renderFooter('stopped')
    fireEvent.click(screen.getByRole('button', { name: 'Start gateway' }))
    expect(start).toHaveBeenCalledTimes(1)
    view.unmount()

    renderFooter('running')
    fireEvent.click(screen.getByRole('button', { name: 'Stop gateway' }))
    expect(stop).toHaveBeenCalledTimes(1)
    expect(start).toHaveBeenCalledTimes(1)
  })
})
