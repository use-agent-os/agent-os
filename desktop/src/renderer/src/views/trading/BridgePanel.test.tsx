import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { BridgePanel } from './BridgePanel'
import { bridgeOrder, ETH, quote, renderDesk, USDC, WALLET } from './test-utils'
import { Ticket } from './Ticket'
import type { BridgeQuote, Token } from './types'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn(async () => {}) } }),
  isDesktop: () => true,
}))

const USDG: Token = {
  chainId: 4663,
  address: '0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168',
  symbol: 'USDG',
  name: 'Global Dollar',
  decimals: 6,
  logoUrl: null,
  native: false,
  verified: true,
}

function bridgeQuote(extra: Partial<BridgeQuote> = {}): BridgeQuote {
  return {
    wallet: WALLET.address,
    originChainId: 8453,
    destinationChainId: 4663,
    tokenIn: ETH,
    tokenOut: { ...ETH, chainId: 4663 },
    amountIn: '0.1',
    expectedOut: '0.0997',
    minOut: '0.0987',
    rate: 0.997,
    feesUsd: 0.12,
    fees: { gasUsd: 0.02, relayerUsd: 0.1, appUsd: 0 },
    timeEstimateSeconds: 2,
    requestId: '0xreq',
    valueUsd: 250,
    fetchedAt: Date.now(),
    provider: 'relay',
    guard: { decision: 'allow', spentTodayUsd: 0, dailyCapUsd: 1000, thresholdUsd: 100 },
    ...extra,
  }
}

function balance(token: Token, amount: string, priceUsd: number) {
  return {
    chainId: token.chainId,
    token,
    raw: '0',
    amount,
    priceUsd,
    valueUsd: Number(amount) * priceUsd,
    change24hPct: 0,
  }
}

function mount(extra: Partial<Parameters<typeof BridgePanel>[0]> = {}) {
  return renderDesk(
    <BridgePanel
      wallets={[WALLET]}
      primary={WALLET.address}
      selectedWallet="all"
      unlocked
      onSent={vi.fn()}
      {...extra}
    />,
  )
}

const amountInput = () => screen.getByRole('textbox', { name: 'Amount' })
const calls = (method: string) => rpcCall.mock.calls.filter((c) => c[0] === method)

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'wallet.balances') {
      return {
        balances: [balance(ETH, '1', 2500), balance(USDC, '40', 1), balance(USDG, '15', 1)],
        updatedAt: Date.now(),
      }
    }
    if (method === 'trading.bridge.quote') return { quote: bridgeQuote() }
    if (method === 'trading.bridge.create') return { order: bridgeOrder({ status: 'submitted' }) }
    if (method === 'trading.quote') return quote()
    return {}
  })
})

describe('BridgePanel', () => {
  it('asks for an amount, then prices the same asset onto the other chain', async () => {
    mount()
    const cta = screen.getByTestId('bridge-review')
    expect(cta).toBeDisabled()
    expect(cta).toHaveTextContent('Enter an amount')
    expect(calls('trading.bridge.quote')).toHaveLength(0)
    // Each leg names its chain; the receive side is the pay side's asset.
    expect(screen.getByLabelText('Base')).toBeInTheDocument()
    expect(screen.getByLabelText('Robinhood Chain')).toBeInTheDocument()
    expect(screen.getByTestId('bridge-token-out')).toHaveTextContent('ETH')

    fireEvent.change(amountInput(), { target: { value: '0.1' } })
    await waitFor(() => expect(screen.getByTestId('bridge-out')).toHaveTextContent('0.0997'))

    expect(calls('trading.bridge.quote')[0]?.[1]).toEqual({
      chainId: 8453,
      toChainId: 4663,
      token: ETH.address,
      amount: '0.1',
      wallet: WALLET.address,
      initiator: 'manual',
    })
    const facts = screen.getByTestId('bridge-facts')
    expect(facts).toHaveTextContent('via Relay')
    expect(facts).toHaveTextContent('Base → Robinhood')
    expect(facts).toHaveTextContent('0.0987 ETH')
    expect(facts).toHaveTextContent('$0.12')
    expect(facts).toHaveTextContent('~2s')
    expect(screen.getByTestId('bridge-guard')).toHaveAttribute('data-decision', 'allow')
    expect(screen.getByTestId('bridge-review')).toHaveTextContent('Review bridge')
    expect(screen.getByTestId('bridge-review')).not.toBeDisabled()
  })

  it('offers ETH and the USD stable only: USDC on Base lands as USDG, and back', async () => {
    mount()
    const asset = screen.getByTestId('bridge-asset')
    expect(
      within(asset)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['ETH', 'USDC'])

    fireEvent.change(asset, { target: { value: 'USD' } })
    expect(screen.getByTestId('bridge-token-out')).toHaveTextContent('USDG')
    await waitFor(() => expect(screen.getByText(/Balance 40/)).toBeInTheDocument())

    // The head picks the chain paid on; the legs trade places.
    fireEvent.click(screen.getByRole('radio', { name: 'Robinhood' }))
    expect(
      within(asset)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['ETH', 'USDG'])
    expect(screen.getByTestId('bridge-token-out')).toHaveTextContent('USDC')
    expect(screen.getByText(/Balance 15/)).toBeInTheDocument()

    fireEvent.change(amountInput(), { target: { value: '5' } })
    await waitFor(() => expect(calls('trading.bridge.quote')).toHaveLength(1))
    expect(calls('trading.bridge.quote')[0]?.[1]).toMatchObject({
      chainId: 4663,
      toChainId: 8453,
      token: USDG.address,
      amount: '5',
    })
  })

  it('the flip reverses the direction and pays what was to arrive', async () => {
    mount()
    fireEvent.change(amountInput(), { target: { value: '0.1' } })
    await waitFor(() => expect(screen.getByTestId('bridge-out')).toHaveTextContent('0.0997'))

    fireEvent.click(screen.getByLabelText('Reverse the direction'))
    expect(screen.getByRole('radio', { name: 'Robinhood' })).toHaveAttribute('aria-checked', 'true')
    expect(amountInput()).toHaveValue('0.0997')
  })

  it('says a shortfall on the button and never asks Relay to price it', async () => {
    mount()
    await waitFor(() => expect(screen.getByText(/Balance 1/)).toBeInTheDocument())
    fireEvent.change(amountInput(), { target: { value: '2' } })
    expect(screen.getByTestId('bridge-review')).toHaveTextContent('Not enough')
    expect(screen.getByTestId('bridge-review')).toBeDisabled()
    expect(calls('trading.bridge.quote')).toHaveLength(0)
  })

  it('Max keeps gas back when the gas coin is what crosses', async () => {
    mount()
    await waitFor(() => expect(screen.getByText('Max')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Max'))
    expect(amountInput()).toHaveValue('0.9997')
    expect(screen.getByTestId('gas-reserve')).toHaveTextContent('0.0003 ETH')
  })

  it('a locked vault quotes nothing', () => {
    mount({ unlocked: false })
    fireEvent.change(amountInput(), { target: { value: '0.1' } })
    expect(screen.getByTestId('bridge-review')).toHaveTextContent('Unlock the vault')
    expect(calls('trading.bridge.quote')).toHaveLength(0)
  })

  it('shows why Relay would not price it', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.bridge.quote') throw new Error('Relay unreachable: timeout')
      return {}
    })
    mount()
    fireEvent.change(amountInput(), { target: { value: '0.1' } })
    expect(await screen.findByRole('alert')).toHaveTextContent('Relay unreachable: timeout')
    expect(screen.getByTestId('bridge-review')).toBeDisabled()
  })

  it('sends only from the confirm sheet, then hands the order on', async () => {
    const onSent = vi.fn()
    mount({ onSent })
    fireEvent.change(amountInput(), { target: { value: '0.1' } })
    await waitFor(() => expect(screen.getByTestId('bridge-review')).not.toBeDisabled())
    fireEvent.click(screen.getByTestId('bridge-review'))
    expect(calls('trading.bridge.create')).toHaveLength(0)

    const sheet = screen.getByRole('alertdialog')
    expect(sheet).toHaveTextContent('Confirm bridge')
    expect(sheet).toHaveTextContent('at least 0.0987 ETH')
    fireEvent.click(screen.getByTestId('confirm-send'))

    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1))
    expect(calls('trading.bridge.create')[0]?.[1]).toEqual({
      chainId: 8453,
      toChainId: 4663,
      token: ETH.address,
      amount: '0.1',
      wallet: WALLET.address,
      initiator: 'manual',
    })
    expect(onSent.mock.calls[0]?.[0]).toHaveLength(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(amountInput()).toHaveValue('')
  })

  it('a large bridge asks for the word before it sends', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'wallet.balances')
        return { balances: [balance(ETH, '10', 2500)], updatedAt: Date.now() }
      if (method === 'trading.bridge.quote')
        return { quote: bridgeQuote({ amountIn: '2', expectedOut: '1.99', valueUsd: 5000 }) }
      return {}
    })
    mount()
    fireEvent.change(amountInput(), { target: { value: '2' } })
    await waitFor(() => expect(screen.getByTestId('bridge-review')).not.toBeDisabled())
    fireEvent.click(screen.getByTestId('bridge-review'))
    expect(screen.getByTestId('confirm-send')).toBeDisabled()
    fireEvent.change(screen.getByTestId('confirm-retype'), { target: { value: 'confirm' } })
    expect(screen.getByTestId('confirm-send')).not.toBeDisabled()
  })
})

describe('Ticket (the full desk)', () => {
  const props = {
    wallets: [WALLET],
    primary: WALLET.address,
    selectedWallet: 'all',
    provider: 'aggregator' as const,
    providerReady: true,
    onOpenSettings: vi.fn(),
    unlocked: true,
    onSent: vi.fn(),
  }

  it('switches between Swap and Bridge in the one head', () => {
    renderDesk(<Ticket {...props} prefill={null} />)
    expect(screen.getByTestId('ticket-mode-swap')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('swap-review')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('ticket-mode-bridge'))
    expect(screen.getByTestId('ticket-mode-bridge')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('bridge-review')).toBeInTheDocument()
    expect(screen.queryByTestId('swap-review')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('ticket-mode-swap'))
    expect(screen.getByTestId('swap-review')).toBeInTheDocument()
  })

  it('a swap asked for from elsewhere brings the swap form back', () => {
    const view = renderDesk(<Ticket {...props} prefill={null} />)
    fireEvent.click(screen.getByTestId('ticket-mode-bridge'))
    expect(screen.getByTestId('bridge-review')).toBeInTheDocument()

    view.rerender(<Ticket {...props} prefill={{ chainId: 8453, tokenIn: USDC, seq: 1 }} />)
    expect(screen.getByTestId('swap-review')).toBeInTheDocument()
    expect(screen.getAllByTestId('token-button')[0]).toHaveTextContent('USDC')
  })
})
