import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthResult } from '@shared/app'
import { DEFAULT_SETTINGS } from '@shared/settings'
import { resetBiometricGateForTests } from '~/lib/biometric-gate'
import { desktopApi } from '~/lib/desktop-api'
import { useSettings } from '~/stores/settings'
import { renderDesk, WALLET } from './test-utils'
import { CLIPBOARD_CLEAR_MS, clearClipboardIf, WalletSheet } from './WalletSheet'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockResolvedValue({})
})

describe('WalletSheet', () => {
  it('creates the vault with a matching password and the chosen unlock mode', async () => {
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'setup' }} onClose={onClose} />)
    const submit = screen.getByTestId('setup-submit')
    expect(submit).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Vault password'), { target: { value: 'short' } })
    expect(screen.getByText('Use at least 8 characters.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Vault password'), {
      target: { value: 'correct horse' },
    })
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'correct hors' },
    })
    expect(screen.getByText('The passwords do not match.')).toBeInTheDocument()
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'correct horse' },
    })
    expect(submit).not.toBeDisabled()

    fireEvent.click(screen.getByLabelText(/Ask me each time/))
    fireEvent.click(submit)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.setup', {
        password: 'correct horse',
        unlockMode: 'manual',
      }),
    )
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('creates a wallet with a label', async () => {
    rpcCall.mockResolvedValue({ wallet: WALLET })
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'create' }} onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'Ops' } })
    fireEvent.click(screen.getByTestId('create-submit'))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('wallet.create', { label: 'Ops' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('imports from a private key', async () => {
    rpcCall.mockResolvedValue({ wallet: WALLET })
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'import' }} onClose={onClose} />)
    const submit = screen.getByTestId('import-submit')
    expect(submit).toBeDisabled()
    const key = `0x${'ab'.repeat(32)}`
    fireEvent.change(screen.getByLabelText('Private key'), { target: { value: key } })
    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'Cold' } })
    fireEvent.click(submit)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.import', { label: 'Cold', privateKey: key }),
    )
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('imports from a keystore with its password', async () => {
    rpcCall.mockResolvedValue({ wallet: WALLET })
    renderDesk(<WalletSheet mode={{ kind: 'import' }} onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Keystore file' }))
    fireEvent.change(screen.getByLabelText('Keystore JSON'), {
      target: { value: '{"version":3}' },
    })
    fireEvent.change(screen.getByLabelText('Keystore password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByTestId('import-submit'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.import', {
        label: 'Imported',
        keystoreJson: '{"version":3}',
        keystorePassword: 'pw',
      }),
    )
  })

  it('exports only after the vault password, then shows and copies the secret once', async () => {
    rpcCall.mockResolvedValue({ privateKey: '0xdeadbeef' })
    const writeText = vi.fn(async () => {})
    Object.assign(navigator, { clipboard: { writeText } })
    renderDesk(<WalletSheet mode={{ kind: 'export', wallet: WALLET }} onClose={vi.fn()} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Anyone with this key controls the funds')
    const reveal = screen.getByTestId('export-reveal')
    expect(reveal).toBeDisabled()
    fireEvent.click(screen.getByRole('radio', { name: 'Raw private key' }))
    fireEvent.change(screen.getByLabelText('Vault password'), {
      target: { value: 'correct horse' },
    })
    fireEvent.click(reveal)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.export', {
        address: WALLET.address,
        password: 'correct horse',
        format: 'privateKey',
      }),
    )
    expect(await screen.findByTestId('export-secret')).toHaveTextContent('0xdeadbeef')
    fireEvent.click(screen.getByTestId('export-copy'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('0xdeadbeef'))
  })

  it('blanks the clipboard a minute after the copy if the key is still there, and closes clean', async () => {
    rpcCall.mockResolvedValue({ privateKey: '0xdeadbeef' })
    const writeText = vi.fn(async () => {})
    const readText = vi.fn(async () => '0xdeadbeef')
    Object.assign(navigator, { clipboard: { writeText, readText } })
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'export', wallet: WALLET }} onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Vault password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByTestId('export-reveal'))
    await screen.findByTestId('export-secret')
    vi.useFakeTimers()
    fireEvent.click(screen.getByTestId('export-copy'))
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('0xdeadbeef'))
    writeText.mockClear()
    await vi.advanceTimersByTimeAsync(CLIPBOARD_CLEAR_MS + 10)
    expect(readText).toHaveBeenCalled()
    expect(writeText).toHaveBeenCalledWith('')
    vi.useRealTimers()
    // A Close beside Copy, which forgets the secret on the way out.
    fireEvent.click(screen.getByTestId('export-close'))
    expect(onClose).toHaveBeenCalled()
  })

  it('leaves the clipboard alone when something else was copied over the key', async () => {
    const writeText = vi.fn(async () => {})
    const readText = vi.fn(async () => 'a grocery list')
    Object.assign(navigator, { clipboard: { writeText, readText } })
    await clearClipboardIf('0xdeadbeef')
    expect(writeText).not.toHaveBeenCalled()
  })

  it('marks a wrong unlock password without closing', async () => {
    rpcCall.mockRejectedValue(new Error('wallet.bad_password'))
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'unlock' }} onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'nope' } })
    fireEvent.click(screen.getByTestId('unlock-submit'))
    expect(await screen.findByText('That password does not open the vault.')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('removes a wallet only with the vault password, and says what it still holds', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'trading.portfolio'
        ? { totals: { valueUsd: 1240.5 }, holdings: [], wallets: [], updatedAt: 0, syncing: false }
        : {},
    )
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'remove', wallet: WALLET }} onClose={onClose} />)
    expect(screen.getByTestId('remove-submit')).toBeDisabled()
    await waitFor(() => expect(screen.getByTestId('remove-worth')).toHaveTextContent('$1,240.50'))
    expect(screen.getByTestId('remove-worth')).toHaveAttribute('data-funded', 'true')
    expect(screen.getByRole('alert')).toHaveTextContent('It still holds funds')
    fireEvent.change(screen.getByLabelText('Vault password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByTestId('remove-submit'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.remove', {
        address: WALLET.address,
        password: 'pw',
      }),
    )
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })
})

describe('WalletSheet · Touch ID', () => {
  beforeEach(() => {
    resetBiometricGateForTests()
    useSettings.setState({
      loaded: true,
      settings: { ...structuredClone(DEFAULT_SETTINGS), security: { touchId: 'high' } },
    })
    return () => {
      useSettings.setState({ settings: structuredClone(DEFAULT_SETTINGS) })
    }
  })

  it('asks before the export, says so on the button, and a cancel sends nothing', async () => {
    let answer: (r: AuthResult) => void = () => {}
    const authenticate = vi.spyOn(desktopApi().app, 'authenticate').mockImplementation(
      () =>
        new Promise<AuthResult>((resolve) => {
          answer = resolve
        }),
    )
    renderDesk(<WalletSheet mode={{ kind: 'export', wallet: WALLET }} onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Raw private key' }))
    const input = screen.getByLabelText('Vault password')
    fireEvent.change(input, { target: { value: 'correct horse' } })
    const reveal = screen.getByTestId('export-reveal')
    fireEvent.click(reveal)
    await waitFor(() => expect(reveal).toHaveTextContent('Touch ID…'))
    expect(authenticate).toHaveBeenCalledWith(
      `export the private key of ${WALLET.label} (0x1111…1111)`,
    )
    // Return in the password field while the sheet is up is nothing.
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(authenticate).toHaveBeenCalledTimes(1)
    await act(async () => answer({ ok: false, reason: 'cancelled' }))
    await waitFor(() => expect(reveal).not.toHaveTextContent('Touch ID…'))
    expect(rpcCall).not.toHaveBeenCalledWith('wallet.export', expect.anything())
    expect(screen.queryByTestId('export-secret')).toBeNull()
    authenticate.mockRestore()
  })

  it('asks before a removal and sends it once the fingerprint matched', async () => {
    const authenticate = vi
      .spyOn(desktopApi().app, 'authenticate')
      .mockResolvedValue({ ok: true } satisfies AuthResult)
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'remove', wallet: WALLET }} onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Vault password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByTestId('remove-submit'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.remove', {
        address: WALLET.address,
        password: 'pw',
      }),
    )
    expect(authenticate).toHaveBeenCalledWith(expect.stringMatching(/^remove the wallet /))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    authenticate.mockRestore()
  })
})

/* The desk's rails are 300–520 px wide and only ever showed a wallet's name,
   so this sheet is where the address and everything that acts on a wallet
   lives. The address must be the whole address: shortening it here would
   reproduce the gap the sheet exists to close. */
describe('WalletSheet · manage', () => {
  const SECOND = {
    ...WALLET,
    address: '0x2222222222222222222222222222222222222222',
    label: '10k',
    primary: false,
  }

  function mockVault(unlocked = true) {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'wallet.list') return { wallets: [WALLET, SECOND], primary: WALLET.address }
      if (method === 'wallet.status')
        return {
          initialized: true,
          unlocked,
          unlockMode: 'session',
          walletCount: 2,
          primary: WALLET.address,
          vaultPath: '/v',
        }
      if (method === 'trading.status')
        return {
          enabled: true,
          chains: [
            {
              chainId: 8453,
              key: 'base',
              name: 'Base',
              native: 'ETH',
              explorer: 'https://basescan.org',
              rpcUrl: '',
              healthy: true,
            },
          ],
        }
      if (method === 'trading.portfolio')
        return { totals: {}, holdings: [], wallets: [], updatedAt: 0, syncing: false }
      return {}
    })
  }

  it('writes every address out in full and copies the whole one', async () => {
    const writeText = vi.fn(async () => {})
    Object.assign(navigator, { clipboard: { writeText } })
    mockVault()
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={vi.fn()} />)

    const rows = await screen.findAllByTestId('manage-address')
    expect(rows.map((n) => n.textContent)).toEqual([WALLET.address, SECOND.address])

    fireEvent.click(screen.getAllByTestId('manage-copy')[1]!)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SECOND.address))
  })

  it('labels every wallet Active and turns on only the primary one', async () => {
    mockVault()
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={vi.fn()} />)
    await screen.findAllByTestId('manage-address')

    const toggles = screen.getAllByTestId('manage-primary')
    expect(toggles.map((n) => n.textContent)).toEqual(['Active', 'Active'])
    expect(toggles.map((n) => n.getAttribute('aria-pressed'))).toEqual(['true', 'false'])
    expect(screen.getAllByTestId('manage-remove')).toHaveLength(2)
    // The enable mark replaced the star, on the name and on the button alike.
    expect(document.querySelector('.lucide-star')).toBeNull()
    expect(screen.getAllByLabelText('Active')).toHaveLength(1)
  })

  it('sends nothing when the wallet that is already active is pressed', async () => {
    mockVault()
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={vi.fn()} />)
    await screen.findAllByTestId('manage-address')

    fireEvent.click(screen.getAllByTestId('manage-primary')[0]!)
    // A mutation starts on a later tick: let one run before saying it did not.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(rpcCall).not.toHaveBeenCalledWith('wallet.setPrimary', expect.anything())
  })

  it('lists the active wallet first, whatever order the vault returns', async () => {
    mockVault()
    const vault = rpcCall.getMockImplementation()!
    rpcCall.mockImplementation(async (method: string, params: unknown) =>
      method === 'wallet.list'
        ? { wallets: [SECOND, WALLET], primary: WALLET.address }
        : vault(method, params),
    )
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={vi.fn()} />)

    const rows = await screen.findAllByTestId('manage-address')
    expect(rows.map((n) => n.textContent)).toEqual([WALLET.address, SECOND.address])
  })

  it('moves a newly activated wallet to the top, and scrolls up to it', async () => {
    mockVault()
    const vault = rpcCall.getMockImplementation()!
    let primary = WALLET.address
    rpcCall.mockImplementation(async (method: string, params: { address: string }) => {
      if (method === 'wallet.setPrimary') {
        primary = params.address
        return {}
      }
      if (method === 'wallet.list')
        return {
          wallets: [WALLET, SECOND].map((w) => ({ ...w, primary: w.address === primary })),
          primary,
        }
      return vault(method, params)
    })
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={vi.fn()} />)
    await screen.findAllByTestId('manage-address')

    // The button is at the bottom of a long list by the time it is pressed.
    const body = document.querySelector('.trd-modal__body')!
    body.scrollTop = 400
    fireEvent.click(screen.getAllByTestId('manage-primary')[1]!)

    await waitFor(() =>
      expect(screen.getAllByTestId('manage-address').map((n) => n.textContent)).toEqual([
        SECOND.address,
        WALLET.address,
      ]),
    )
    expect(rpcCall).toHaveBeenCalledWith('wallet.setPrimary', { address: SECOND.address })
    expect(body.scrollTop).toBe(0)
  })

  it('greys out Export and Remove while the vault is locked, and says why', async () => {
    mockVault(false)
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={vi.fn()} />)
    await screen.findAllByTestId('manage-address')
    await waitFor(() => expect(screen.getAllByTestId('manage-export')[0]).toBeDisabled())
    expect(screen.getAllByTestId('manage-remove')[0]).toBeDisabled()
    expect(screen.getAllByTestId('manage-export')[0]).toHaveAttribute(
      'title',
      'Unlock the vault first',
    )
    // Rename needs no key: still live.
    expect(screen.getAllByTestId('manage-rename')[0]).not.toBeDisabled()
  })

  it('opens a QR of the address, drawn here rather than fetched', async () => {
    mockVault()
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={vi.fn()} />)
    await screen.findAllByTestId('manage-address')

    fireEvent.click(screen.getAllByTestId('manage-qr')[0]!)

    expect(screen.getByTestId('receive-address').textContent).toBe(WALLET.address)
    const qr = screen.getByRole('img', { name: /QR code/i })
    // A `data:` image: nothing was asked of a QR web service, which would have
    // disclosed the address and been blocked by the window's CSP anyway.
    expect(qr.getAttribute('src')).toMatch(/^data:image\/svg\+xml;base64,/)
  })

  it('returns to the manager when a flow it opened is closed', async () => {
    mockVault()
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'manage' }} onClose={onClose} />)
    await screen.findAllByTestId('manage-address')

    fireEvent.click(screen.getAllByTestId('manage-rename')[0]!)
    expect(screen.getByText('Rename wallet')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Cancel'))
    await screen.findAllByTestId('manage-address')
    // Closing the sub-flow must not close the sheet the caller opened.
    expect(onClose).not.toHaveBeenCalled()
  })
})
