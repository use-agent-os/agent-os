import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Holdings } from './Holdings'
import { checkSendHolding, SendHolding } from './SendHolding'
import { ETH, holding, renderDesk, USDC, WALLET } from './test-utils'
import type { Wallet } from './types'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn(async () => {}) } }),
  isDesktop: () => true,
}))
const toasts = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: toasts }))

const TO = '0x2222222222222222222222222222222222222222'
// An EIP-55 vector, and the same with one letter's case flipped.
const GOOD = '0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed'
const MANGLED = '0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed'
const SECOND: Wallet = {
  address: '0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed',
  label: 'Vault',
  primary: false,
  createdAt: 1_700_000_000_001,
  chains: [8453, 4663],
}

const sendCalls = () => rpcCall.mock.calls.filter((c) => c[0] === 'trading.send')
const to = () => screen.getByTestId('holding-send-to') as HTMLInputElement
const amount = () => screen.getByTestId('holding-send-amount') as HTMLInputElement
const sendNow = () => screen.getByTestId('holding-send-now')

beforeEach(() => {
  rpcCall.mockReset()
  toasts.success.mockReset()
  toasts.error.mockReset()
})

function mount(h = holding(), wallets: Wallet[] = [WALLET, SECOND]) {
  const onClose = vi.fn()
  const onSent = vi.fn()
  renderDesk(<SendHolding holding={h} wallets={wallets} onClose={onClose} onSent={onSent} />)
  return { onClose, onSent }
}

describe('Holdings · send', () => {
  it('offers Send on a row, left of Swap, and hands the holding over', () => {
    const onSend = vi.fn()
    const h = holding()
    renderDesk(
      <Holdings
        holdings={[h]}
        loading={false}
        selected={null}
        onSelect={() => {}}
        onSwap={() => {}}
        onSend={onSend}
        showChain
      />,
    )
    const send = screen.getByRole('button', { name: 'Send USDC' })
    const swap = screen.getByRole('button', { name: 'Swap USDC' })
    expect(send.compareDocumentPosition(swap) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledWith(h)
  })

  it('offers no Send without a handler, nor on a junk row', () => {
    renderDesk(
      <Holdings
        holdings={[holding({ hidden: true })]}
        loading={false}
        selected={null}
        onSelect={() => {}}
        onSwap={() => {}}
        onSend={() => {}}
        showChain
        showHidden
      />,
    )
    expect(screen.queryByTestId('holding-send-open')).toBeNull()
  })
})

describe('SendHolding', () => {
  it('names the token, the paying wallet and the balance', () => {
    mount()
    const sheet = screen.getByTestId('holding-send')
    expect(screen.getByRole('dialog', { name: 'Send USDC' })).toBeTruthy()
    expect(sheet.textContent).toContain('Main')
    expect(screen.getByTestId('holding-send-balance').textContent).toContain('900 USDC')
  })

  it('refuses to send until the address and the amount are whole', () => {
    mount()
    fireEvent.click(sendNow())
    expect(screen.getByTestId('holding-send-error').textContent).toBe('That is not an address')
    fireEvent.change(to(), { target: { value: TO } })
    fireEvent.click(sendNow())
    expect(screen.getByTestId('holding-send-error').textContent).toBe('Enter an amount')
    fireEvent.change(amount(), { target: { value: '901' } })
    fireEvent.click(sendNow())
    expect(screen.getByTestId('holding-send-error').textContent).toBe('More than this wallet holds')
    expect(sendCalls()).toHaveLength(0)
  })

  it('fills the amount from the percent buttons', () => {
    mount()
    fireEvent.click(screen.getByTestId('holding-send-pct-25'))
    expect(amount().value).toBe('225')
    fireEvent.click(screen.getByTestId('holding-send-pct-50'))
    expect(amount().value).toBe('450')
    fireEvent.click(screen.getByTestId('holding-send-pct-75'))
    expect(amount().value).toBe('675')
    fireEvent.click(screen.getByTestId('holding-send-pct-100'))
    expect(amount().value).toBe('900')
  })

  it('keeps gas back when the holding is the gas coin', () => {
    mount(holding({ token: ETH, amount: '1', raw: '1000000000000000000', priceUsd: 2500 }))
    expect(screen.getByTestId('holding-send-reserve').textContent).toContain('0.0003')
    fireEvent.click(screen.getByTestId('holding-send-pct-100'))
    expect(amount().value).toBe('0.9997')
    fireEvent.change(to(), { target: { value: TO } })
    fireEvent.change(amount(), { target: { value: '1' } })
    fireEvent.click(sendNow())
    expect(screen.getByTestId('holding-send-error').textContent).toBe('Leave some for gas')
  })

  it('picks one of my other wallets as the recipient, checksummed', () => {
    mount()
    const own = screen.getAllByTestId('holding-send-own')
    expect(own).toHaveLength(1)
    expect(own[0]?.textContent).toBe('Vault')
    fireEvent.click(own[0] as HTMLElement)
    expect(to().value).toBe(GOOD)
  })

  it('refuses a mangled checksum and the paying wallet itself', () => {
    mount()
    fireEvent.change(amount(), { target: { value: '1' } })
    fireEvent.change(to(), { target: { value: MANGLED } })
    fireEvent.click(sendNow())
    expect(screen.getByTestId('holding-send-error').textContent).toContain('Checksum')
    fireEvent.change(to(), { target: { value: WALLET.address } })
    expect(screen.getByTestId('holding-send-error').textContent).toBe(
      'That is the wallet you are sending from',
    )
    expect(sendCalls()).toHaveLength(0)
  })

  it('sends only on the second click, as the user, from the row wallet', async () => {
    rpcCall.mockResolvedValue({ orders: [{ orderId: 'o1', status: 'submitted' }], batchId: null })
    const { onClose, onSent } = mount()
    fireEvent.change(to(), { target: { value: TO } })
    fireEvent.change(amount(), { target: { value: '12.5' } })
    fireEvent.click(sendNow())
    expect(sendCalls()).toHaveLength(0)
    const review = screen.getByTestId('holding-send-review')
    expect(review.textContent).toContain(TO)
    expect(review.textContent).toContain('12.5 USDC')
    expect(sendNow().textContent).toBe('Click again to send')
    fireEvent.click(sendNow())
    await waitFor(() => expect(sendCalls()).toHaveLength(1))
    expect(sendCalls()[0]?.[1]).toEqual({
      chainId: 8453,
      wallet: WALLET.address,
      token: USDC.address,
      recipients: [{ to: TO, amount: '12.5' }],
      initiator: 'manual',
    })
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(onSent).toHaveBeenCalled()
    expect(toasts.success).toHaveBeenCalled()
  })

  it('disarms when the form changes after the first click', () => {
    mount()
    fireEvent.change(to(), { target: { value: TO } })
    fireEvent.change(amount(), { target: { value: '1' } })
    fireEvent.click(sendNow())
    expect(screen.getByTestId('holding-send-review')).toBeTruthy()
    fireEvent.change(amount(), { target: { value: '2' } })
    expect(screen.queryByTestId('holding-send-review')).toBeNull()
    expect(sendNow().textContent).toBe('Send now')
  })

  it('stays open and says why when the send failed', async () => {
    rpcCall.mockResolvedValue({
      orders: [{ orderId: 'o1', status: 'failed', reason: 'transfer reverted' }],
      batchId: null,
    })
    const { onClose } = mount()
    fireEvent.change(to(), { target: { value: TO } })
    fireEvent.change(amount(), { target: { value: '1' } })
    fireEvent.click(sendNow())
    fireEvent.click(sendNow())
    await waitFor(() => expect(toasts.error).toHaveBeenCalled())
    expect(String(toasts.error.mock.calls[0]?.[0])).toContain('transfer reverted')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('falls back to the given wallet when the row names none', async () => {
    rpcCall.mockResolvedValue({ orders: [], batchId: null })
    renderDesk(
      <SendHolding
        holding={holding({ wallet: null })}
        wallets={[WALLET, SECOND]}
        fallbackWallet={SECOND.address}
        onClose={() => {}}
      />,
    )
    fireEvent.change(to(), { target: { value: TO } })
    fireEvent.change(amount(), { target: { value: '1' } })
    fireEvent.click(sendNow())
    fireEvent.click(sendNow())
    await waitFor(() => expect(sendCalls()).toHaveLength(1))
    expect(sendCalls()[0]?.[1]).toMatchObject({ wallet: SECOND.address })
  })
})

describe('checkSendHolding', () => {
  const base = { from: WALLET.address, balance: '10', spendable: '10', decimals: 6 }
  it('passes a whole form and names the first problem otherwise', () => {
    expect(checkSendHolding({ ...base, to: TO, amount: '10' })).toBeNull()
    expect(checkSendHolding({ ...base, to: '0x12', amount: '1' })).toBe('address')
    expect(checkSendHolding({ ...base, to: TO, amount: '0' })).toBe('amount')
    expect(checkSendHolding({ ...base, to: TO, amount: 'abc' })).toBe('amount')
    expect(checkSendHolding({ ...base, to: TO, amount: '10.000001' })).toBe('insufficient')
    expect(checkSendHolding({ ...base, to: TO, amount: '10', spendable: '9' })).toBe('gas')
  })
})
