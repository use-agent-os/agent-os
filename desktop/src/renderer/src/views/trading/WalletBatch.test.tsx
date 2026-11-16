import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthResult } from '@shared/app'
import { DEFAULT_SETTINGS } from '@shared/settings'
import { resetBiometricGateForTests } from '~/lib/biometric-gate'
import { desktopApi } from '~/lib/desktop-api'
import { useSettings } from '~/stores/settings'
import { renderDesk, WALLET } from './test-utils'
import { parseKeyRows, walletsCsv, walletsCsvName } from './wallet-batch'
import { WalletSheet } from './WalletSheet'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))

const A = '0xAbCdEf0000000000000000000000000000001234'
const B = '0x9999999999999999999999999999999999995678'
const SECRETS = [
  { address: A, privateKey: `0x${'aa'.repeat(32)}`, mnemonic: 'one two three' },
  { address: B, privateKey: `0x${'bb'.repeat(32)}`, mnemonic: 'four five six' },
]
const BATCH = {
  batchId: 'batch-1',
  expiresInS: 900,
  wallets: [
    { ...WALLET, address: A, label: '0xAbC_1234', primary: false },
    { ...WALLET, address: B, label: '0x999_5678', primary: false },
  ],
}

let saved: Blob[] = []

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'wallet.createBatch') return BATCH
    if (method === 'wallet.exportBatch') return { wallets: SECRETS }
    return {}
  })
  saved = []
  URL.createObjectURL = vi.fn((blob: Blob) => {
    saved.push(blob)
    return 'blob:csv'
  })
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  resetBiometricGateForTests()
  useSettings.setState({ loaded: true, settings: structuredClone(DEFAULT_SETTINGS) })
})

afterEach(() => {
  vi.restoreAllMocks()
})

async function createTwo(): Promise<void> {
  renderDesk(<WalletSheet mode={{ kind: 'create' }} onClose={vi.fn()} />)
  fireEvent.click(screen.getByTestId('create-tab-bulk'))
  fireEvent.change(screen.getByLabelText('Number of wallets'), { target: { value: '2' } })
  fireEvent.click(screen.getByTestId('create-bulk-submit'))
  await waitFor(() =>
    expect(rpcCall).toHaveBeenCalledWith('wallet.createBatch', { count: 2, label: '' }),
  )
  await screen.findByTestId('batch-wallets')
}

describe('wallet-batch helpers', () => {
  it('writes no,address,private_key,seed_phrase in batch order', () => {
    expect(walletsCsv(SECRETS)).toBe(
      'no,address,private_key,seed_phrase\r\n' +
        `1,${A},0x${'aa'.repeat(32)},one two three\r\n` +
        `2,${B},0x${'bb'.repeat(32)},four five six\r\n`,
    )
    expect(walletsCsv([{ address: 'a,b', privateKey: 'k"1', mnemonic: 'm' }])).toContain(
      '1,"a,b","k""1",m',
    )
  })

  it('reads one key per row and remembers each row’s line', () => {
    expect(parseKeyRows('  0x1 \n\n0x2\r\n   \n0x3')).toEqual([
      { line: 1, key: '0x1' },
      { line: 3, key: '0x2' },
      { line: 5, key: '0x3' },
    ])
    expect(parseKeyRows('\n \n')).toEqual([])
  })

  it('names the file by the local date and time', () => {
    expect(walletsCsvName(new Date(2026, 10, 6, 9, 5))).toBe('agentos-wallets-20261106-0905.csv')
  })
})

describe('Create new · one wallet', () => {
  it('sends an empty label so the engine names it after the address', async () => {
    rpcCall.mockResolvedValue({ wallet: WALLET })
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'create' }} onClose={onClose} />)
    expect(screen.getByLabelText('Label')).toHaveAttribute(
      'placeholder',
      'Optional — e.g. 0xAbC_1234',
    )
    fireEvent.click(screen.getByTestId('create-submit'))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledWith('wallet.create', { label: '' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })
})

describe('Create new · bulk', () => {
  it('takes 1 to 30 wallets and nothing else', () => {
    renderDesk(<WalletSheet mode={{ kind: 'create' }} onClose={vi.fn()} />)
    fireEvent.click(screen.getByTestId('create-tab-bulk'))
    const input = screen.getByLabelText('Number of wallets')
    const submit = screen.getByTestId('create-bulk-submit')
    expect(submit).toHaveTextContent('Create 10 wallets')
    for (const bad of ['31', '0', '2.5', '']) {
      fireEvent.change(input, { target: { value: bad } })
      expect(submit, bad).toBeDisabled()
    }
    fireEvent.change(input, { target: { value: '30' } })
    expect(submit).not.toBeDisabled()
    expect(submit).toHaveTextContent('Create 30 wallets')
  })

  it('numbers the wallets after a prefix', async () => {
    renderDesk(<WalletSheet mode={{ kind: 'create' }} onClose={vi.fn()} />)
    fireEvent.click(screen.getByTestId('create-tab-bulk'))
    fireEvent.change(screen.getByLabelText('Label prefix'), { target: { value: ' Bot ' } })
    fireEvent.click(screen.getByTestId('create-bulk-submit'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.createBatch', { count: 10, label: 'Bot' }),
    )
  })

  it('lists the new wallets and downloads the CSV only after the vault password', async () => {
    await createTwo()
    expect(screen.getByText('2 new wallets')).toBeInTheDocument()
    expect(screen.getByTestId('batch-wallets')).toHaveTextContent(A)
    expect(screen.getByTestId('batch-wallets')).toHaveTextContent('0x999_5678')
    // Nothing secret is fetched until the operator confirms.
    expect(rpcCall).not.toHaveBeenCalledWith('wallet.exportBatch', expect.anything())
    const download = screen.getByTestId('batch-download')
    expect(download).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Vault password'), { target: { value: 'pw' } })
    fireEvent.click(download)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.exportBatch', {
        batchId: 'batch-1',
        password: 'pw',
      }),
    )
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(await saved[0]!.text()).toBe(walletsCsv(SECRETS))
    expect(screen.getByLabelText('Vault password')).toHaveValue('')
    expect(screen.getByTestId('batch-done')).toHaveTextContent('Done')
  })

  it('downloads with Touch ID instead, whatever Settings › Security says', async () => {
    vi.spyOn(desktopApi().app, 'biometrics').mockResolvedValue({ available: true })
    const authenticate = vi
      .spyOn(desktopApi().app, 'authenticate')
      .mockResolvedValue({ ok: true } satisfies AuthResult)
    await createTwo()
    fireEvent.click(await screen.findByTestId('batch-touchid'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.exportBatch', {
        batchId: 'batch-1',
        touchId: true,
      }),
    )
    expect(authenticate).toHaveBeenCalledTimes(1)
    expect(authenticate).toHaveBeenCalledWith('download the keys of the wallets just created')
    await waitFor(() => expect(saved).toHaveLength(1))
  })

  it('sends nothing when Touch ID is cancelled', async () => {
    vi.spyOn(desktopApi().app, 'biometrics').mockResolvedValue({ available: true })
    vi.spyOn(desktopApi().app, 'authenticate').mockResolvedValue({
      ok: false,
      reason: 'cancelled',
    } satisfies AuthResult)
    await createTwo()
    fireEvent.click(await screen.findByTestId('batch-touchid'))
    await waitFor(() => expect(screen.getByTestId('batch-touchid')).not.toBeDisabled())
    expect(rpcCall).not.toHaveBeenCalledWith('wallet.exportBatch', expect.anything())
    expect(saved).toHaveLength(0)
  })

  it('offers no Touch ID button on a Mac without it', async () => {
    await createTwo()
    expect(screen.queryByTestId('batch-touchid')).toBeNull()
  })

  it('says so when the keys are no longer held', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'wallet.createBatch') return BATCH
      if (method === 'wallet.exportBatch')
        throw Object.assign(new Error('no longer held'), { code: 'wallet.batch_expired' })
      return {}
    })
    await createTwo()
    fireEvent.change(screen.getByLabelText('Vault password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByTestId('batch-download'))
    await screen.findByTestId('batch-expired')
    expect(screen.queryByTestId('batch-download')).toBeNull()
    expect(saved).toHaveLength(0)
  })
})

describe('Import Wallets · bulk', () => {
  const K1 = `0x${'11'.repeat(32)}`
  const K2 = `0x${'22'.repeat(32)}`
  const K3 = `${'33'.repeat(32)}`

  it('is titled Import Wallets and has a Bulk import tab', () => {
    renderDesk(<WalletSheet mode={{ kind: 'import' }} onClose={vi.fn()} />)
    expect(screen.getByRole('heading', { name: 'Import Wallets' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Bulk import' })).toBeInTheDocument()
  })

  it('imports every row and closes when all went in', async () => {
    rpcCall.mockResolvedValue({
      imported: 3,
      results: [0, 1, 2].map((index) => ({ index, status: 'imported' })),
    })
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'import' }} onClose={onClose} />)
    fireEvent.click(screen.getByTestId('import-tab-bulk'))
    const submit = screen.getByTestId('import-bulk-submit')
    expect(submit).toBeDisabled()
    expect(submit).toHaveTextContent('Import wallets')
    fireEvent.change(screen.getByLabelText('Private keys'), {
      target: { value: `${K1}\n\n  ${K2}  \n${K3}\n` },
    })
    expect(screen.getByTestId('import-bulk-count')).toHaveTextContent('3 keys')
    fireEvent.click(submit)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('wallet.importBatch', { privateKeys: [K1, K2, K3] }),
    )
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('keeps the rows that did not go in and names their lines, never the keys', async () => {
    rpcCall.mockResolvedValue({
      imported: 1,
      results: [
        { index: 0, status: 'imported' },
        { index: 1, status: 'exists' },
        { index: 2, status: 'invalid', error: 'private key must be hex' },
      ],
    })
    const onClose = vi.fn()
    renderDesk(<WalletSheet mode={{ kind: 'import' }} onClose={onClose} />)
    fireEvent.click(screen.getByTestId('import-tab-bulk'))
    fireEvent.change(screen.getByLabelText('Private keys'), {
      target: { value: `${K1}\n\n${K2}\nnot-a-key` },
    })
    fireEvent.click(screen.getByTestId('import-bulk-submit'))
    const failures = await screen.findByTestId('import-bulk-failures')
    // Numbered as the rows that are left in the box.
    expect(failures).toHaveTextContent('Row 1: already in the vault')
    expect(failures).toHaveTextContent('Row 2: private key must be hex')
    expect(failures).not.toHaveTextContent(K2)
    expect(screen.getByLabelText('Private keys')).toHaveValue(`${K2}\nnot-a-key`)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('refuses more than 100 keys at once', () => {
    renderDesk(<WalletSheet mode={{ kind: 'import' }} onClose={vi.fn()} />)
    fireEvent.click(screen.getByTestId('import-tab-bulk'))
    fireEvent.change(screen.getByLabelText('Private keys'), {
      target: { value: Array.from({ length: 101 }, () => K1).join('\n') },
    })
    expect(screen.getByText('At most 100 keys at a time.')).toBeInTheDocument()
    expect(screen.getByTestId('import-bulk-submit')).toBeDisabled()
  })
})
