import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_SETTINGS, type GatewaySettings, type SettingsPatch } from '@shared/settings'
import { useSettings } from '~/stores/settings'
import { GatewayPane } from './GatewayPane'

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

beforeEach(() => {
  update.mockClear()
  useSettings.setState({
    loaded: true,
    settings: { ...structuredClone(DEFAULT_SETTINGS), gateway: { ...REMOTE } },
    update,
  })
})

function field(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement
}

describe('GatewayPane run mode', () => {
  it('moves a remote endpoint back to the local gateway when switched to Managed', () => {
    render(<GatewayPane />)
    expect(field('Host').value).toBe('pn.capminal.ai')

    fireEvent.click(screen.getByRole('radio', { name: 'Managed' }))
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
    fireEvent.click(screen.getByRole('radio', { name: 'Managed' }))
    fireEvent.click(screen.getByRole('radio', { name: 'External' }))
    expect(field('Host').value).toBe('pn.capminal.ai')
    expect(field('Port').value).toBe('443')
    expect(screen.getByRole('radio', { name: 'HTTPS' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
})
