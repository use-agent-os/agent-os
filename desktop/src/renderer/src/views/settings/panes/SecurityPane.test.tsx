import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthResult, BiometricsInfo } from '@shared/app'
import { DEFAULT_SETTINGS, type SettingsPatch, type TouchIdMode } from '@shared/settings'
import { useSettings } from '~/stores/settings'
import { SecurityPane } from './SecurityPane'

const biometrics = vi.fn<() => Promise<BiometricsInfo>>()
const authenticate = vi.fn<(reason: string) => Promise<AuthResult>>()
const update = vi.fn<(patch: SettingsPatch) => Promise<void>>(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { biometrics, authenticate } }),
  isDesktop: () => true,
}))

function setMode(touchId: TouchIdMode): void {
  useSettings.setState({
    loaded: true,
    settings: { ...structuredClone(DEFAULT_SETTINGS), security: { touchId } },
    update,
  })
}

/** A mode card: its accessible name starts with the title, then its sentence. */
function radio(name: string): HTMLElement {
  return screen.getByRole('radio', { name: new RegExp(`^${name}`) })
}

beforeEach(() => {
  biometrics.mockReset()
  authenticate.mockReset()
  update.mockClear()
  setMode('off')
})

describe('SecurityPane', () => {
  it('offers the three modes and saves the one picked', async () => {
    biometrics.mockResolvedValue({ available: true })
    render(<SecurityPane />)
    expect(screen.getByRole('heading', { name: 'Security' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('touch-id-test')).not.toBeDisabled())
    expect(radio('Off')).toHaveAttribute('aria-checked', 'true')
    expect(radio('High-risk approvals')).not.toBeDisabled()
    fireEvent.click(radio('High-risk approvals'))
    expect(update).toHaveBeenCalledWith({ security: { touchId: 'high' } })
    fireEvent.click(radio('Every approval'))
    expect(update).toHaveBeenLastCalledWith({ security: { touchId: 'all' } })
    expect(screen.queryByText(/no Touch ID sensor/)).toBeNull()
  })

  it('is disabled with an explanation on a Mac without Touch ID', async () => {
    biometrics.mockResolvedValue({ available: false })
    render(<SecurityPane />)
    await waitFor(() =>
      expect(
        screen.getByText('This Mac has no Touch ID sensor (or the lid is closed).'),
      ).toBeInTheDocument(),
    )
    for (const name of ['Off', 'High-risk approvals', 'Every approval'])
      expect(radio(name)).toBeDisabled()
    expect(screen.getByTestId('touch-id-test')).toBeDisabled()
  })

  it('never locks a mode that is on: it can always be turned off without the sensor', async () => {
    setMode('high')
    biometrics.mockResolvedValue({ available: false })
    render(<SecurityPane />)
    await waitFor(() =>
      expect(screen.getByText(/Touch ID is unavailable right now/)).toBeInTheDocument(),
    )
    expect(radio('Off')).not.toBeDisabled()
    fireEvent.click(radio('Off'))
    expect(update).toHaveBeenCalledWith({ security: { touchId: 'off' } })
    expect(authenticate).not.toHaveBeenCalled()
  })

  it('Test prompts and reports what came back', async () => {
    biometrics.mockResolvedValue({ available: true })
    let answer: (r: AuthResult) => void = () => {}
    authenticate.mockImplementation(
      () =>
        new Promise<AuthResult>((resolve) => {
          answer = resolve
        }),
    )
    render(<SecurityPane />)
    const test = screen.getByTestId('touch-id-test')
    await waitFor(() => expect(test).not.toBeDisabled())
    fireEvent.click(test)
    expect(authenticate).toHaveBeenCalledWith('test Touch ID')
    await waitFor(() => expect(test).toHaveTextContent('Touch ID…'))
    await act(async () => answer({ ok: false, reason: 'cancelled' }))
    expect(screen.getByText('The prompt was dismissed.')).toBeInTheDocument()
    authenticate.mockResolvedValue({ ok: true })
    fireEvent.click(test)
    await waitFor(() =>
      expect(screen.getByText('Touch ID confirmed it is you.')).toBeInTheDocument(),
    )
  })
})
