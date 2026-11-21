import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_GATEWAY_SETTINGS,
  DEFAULT_SETTINGS,
  type GatewaySettings,
  type SettingsPatch,
} from '@shared/settings'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { useDraftStore } from '../memory/drafts'
import { GatewayPane, onGatewayDefaults } from './GatewayPane'

const update = vi.fn<(patch: SettingsPatch) => Promise<void>>(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: {} }),
  isDesktop: () => true,
}))

const REMOTE: GatewaySettings = {
  mode: 'external',
  scheme: 'https',
  host: 'pn.capminal.ai',
  port: 443,
  token: 'remote-token',
  cliPath: null,
}

const start = vi.fn(async () => {})
const stop = vi.fn(async () => {})
const restart = vi.fn(async () => {})

beforeEach(() => {
  useDraftStore.setState({ drafts: {} })
  update.mockClear()
  start.mockClear()
  stop.mockClear()
  restart.mockClear()
  useGateway.setState({
    status: { state: 'running', pid: null, url: 'https://pn.capminal.ai:443', error: null },
    busy: false,
    start,
    stop,
    restart,
  })
  useSettings.setState({
    loaded: true,
    settings: { ...structuredClone(DEFAULT_SETTINGS), gateway: { ...REMOTE } },
    update,
  })
})

function field(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement
}

/** A run-mode card: its name starts with the title, then its sentence. */
function mode(name: 'Managed' | 'External'): HTMLElement {
  return screen.getByRole('radio', { name: new RegExp(`^${name}`) })
}

function managed(patch: Partial<GatewaySettings> = {}) {
  useSettings.setState({
    loaded: true,
    settings: {
      ...structuredClone(DEFAULT_SETTINGS),
      gateway: { ...DEFAULT_GATEWAY_SETTINGS, ...patch },
    },
    update,
  })
}

describe('GatewayPane run mode', () => {
  it('moves a remote endpoint back to the local gateway when switched to Managed', () => {
    render(<GatewayPane />)
    expect(field('Host').value).toBe('pn.capminal.ai')

    fireEvent.click(mode('Managed'))
    expect(field('Host').value).toBe('127.0.0.1')
    expect(field('Port').value).toBe('18791')
    expect(screen.getByRole('radio', { name: 'HTTP' })).toHaveAttribute('aria-checked', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(update).toHaveBeenCalledWith({
      gateway: {
        ...REMOTE,
        mode: 'managed',
        scheme: 'http',
        host: '127.0.0.1',
        port: 18791,
      },
    })
  })

  it('brings the external endpoint back when switched to External again before saving', () => {
    render(<GatewayPane />)
    fireEvent.click(mode('Managed'))
    fireEvent.click(mode('External'))
    expect(field('Host').value).toBe('pn.capminal.ai')
    expect(field('Port').value).toBe('443')
    expect(screen.getByRole('radio', { name: 'HTTPS' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
})

describe('GatewayPane status', () => {
  it('leads with the state and offers Stop and Restart for a gateway it runs', () => {
    managed()
    useGateway.setState({
      status: { state: 'running', pid: 4242, url: 'http://127.0.0.1:18791', error: null },
    })
    render(<GatewayPane />)
    const status = screen.getByRole('group', { name: 'Status' })
    expect(status).toHaveTextContent('Gateway running')
    expect(status).toHaveTextContent('http://127.0.0.1:18791')
    expect(status).toHaveTextContent('Started by this app · pid 4242')
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(stop).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect(restart).toHaveBeenCalledTimes(1)
  })

  it('offers Start when the managed gateway is off', () => {
    managed()
    useGateway.setState({ status: { state: 'stopped', pid: null, url: null, error: null } })
    render(<GatewayPane />)
    expect(screen.getByRole('group', { name: 'Status' })).toHaveTextContent('Gateway off')
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(start).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  })

  it('has no start or stop for an external gateway: the app only connects', () => {
    render(<GatewayPane />)
    expect(screen.getByRole('group', { name: 'Status' })).toHaveTextContent(
      'External gateway: the app only connects to it',
    )
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull()
  })
})

describe('GatewayPane connection', () => {
  it('folds the address of a managed gateway on its defaults under one line', () => {
    managed()
    render(<GatewayPane />)
    expect(mode('Managed')).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByLabelText('Host')).toBeNull()
    const summary = screen.getByRole('button', { name: /Address and token/ })
    expect(summary).toHaveTextContent('http://127.0.0.1:18791 · no token')
    fireEvent.click(summary)
    expect(field('Host').value).toBe('127.0.0.1')
    expect(field('Port').value).toBe('18791')
    expect(screen.getByLabelText('Command-line tool')).toBeInTheDocument()
  })

  it('shows the address as soon as anything differs from the defaults', () => {
    managed({ port: 18800 })
    render(<GatewayPane />)
    expect(field('Port').value).toBe('18800')
    expect(screen.queryByRole('button', { name: /Address and token/ })).toBeNull()
  })

  it('keeps the rows on screen while the run mode is being changed', () => {
    managed()
    render(<GatewayPane />)
    fireEvent.click(screen.getByRole('button', { name: /Address and token/ }))
    fireEvent.click(mode('External'))
    // Not folded away mid-edit; the command-line tool only matters when managed.
    expect(field('Host').value).toBe('127.0.0.1')
    expect(screen.queryByLabelText('Command-line tool')).toBeNull()
  })

  it('knows what the defaults are', () => {
    expect(onGatewayDefaults(DEFAULT_GATEWAY_SETTINGS)).toBe(true)
    expect(onGatewayDefaults({ ...DEFAULT_GATEWAY_SETTINGS, token: 'secret' })).toBe(false)
    expect(onGatewayDefaults({ ...DEFAULT_GATEWAY_SETTINGS, cliPath: '/opt/agentos' })).toBe(false)
    expect(onGatewayDefaults(REMOTE)).toBe(false)
  })

  it('keeps an unsaved address when the page is left and opened again, until reverted', () => {
    const first = render(<GatewayPane />)
    fireEvent.change(field('Host'), { target: { value: 'gw.example.com' } })
    first.unmount()
    render(<GatewayPane />)
    expect(field('Host')).toHaveValue('gw.example.com')
    fireEvent.click(screen.getByRole('button', { name: 'Revert' }))
    expect(field('Host')).toHaveValue('pn.capminal.ai')
    expect(useDraftStore.getState().drafts).toEqual({})
  })
})
