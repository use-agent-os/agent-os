import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ConfirmSwap } from './ConfirmSwap'
import { SwapPanel } from './SwapPanel'
import { ETH, quote, renderDesk, USDC, WALLET } from './test-utils'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn(async () => {}) } }),
  isDesktop: () => true,
}))

function mount(extra: Partial<Parameters<typeof SwapPanel>[0]> = {}) {
  return renderDesk(
    <SwapPanel
      wallets={[WALLET]}
      primary={WALLET.address}
      selectedWallet="all"
      provider="uniswap"
      providerReady
      onOpenSettings={vi.fn()}
      unlocked
      prefill={{ chainId: 8453, tokenIn: ETH, tokenOut: USDC, seq: 1 }}
      onSent={vi.fn()}
      {...extra}
    />,
  )
}

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string) => {
    if (method === 'wallet.balances') {
      return {
        balances: [
          {
            chainId: 8453,
            token: ETH,
            raw: '1000000000000000000',
            amount: '1',
            priceUsd: 2500,
            valueUsd: 2500,
            change24hPct: 1.2,
          },
        ],
        updatedAt: Date.now(),
      }
    }
    if (method === 'trading.quote') return quote()
    return {}
  })
})

describe('SwapPanel', () => {
  it('asks for an amount before it quotes, then shows the price, facts and guard', async () => {
    mount()
    const cta = screen.getByTestId('swap-review')
    expect(cta).toBeDisabled()
    expect(cta).toHaveTextContent('Enter an amount')
    expect(rpcCall.mock.calls.some((c) => c[0] === 'trading.quote')).toBe(false)

    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '0.1' } })
    await waitFor(() => expect(screen.getByTestId('quote-out')).toHaveTextContent('250.12'))

    const call = rpcCall.mock.calls.find((c) => c[0] === 'trading.quote')
    expect(call?.[1]).toMatchObject({
      chainId: 8453,
      wallet: WALLET.address,
      tokenIn: ETH.address,
      tokenOut: USDC.address,
      amountIn: '0.1',
      initiator: 'manual',
    })
    expect(screen.getByTestId('quote-facts')).toHaveTextContent('Minimum received')
    expect(screen.getByTestId('quote-facts')).toHaveTextContent('248.87 USDC')
    expect(screen.getByTestId('quote-guard')).toHaveAttribute('data-decision', 'allow')
    expect(screen.getByTestId('swap-review')).not.toBeDisabled()
    expect(screen.getByTestId('swap-review')).toHaveTextContent('Review swap')
  })

  it('keeps a gas reserve back from Max and 100% when paying in ETH', async () => {
    mount()
    await screen.findByText(/Balance/)
    const note = screen.getByTestId('gas-reserve')
    expect(note).toHaveTextContent('Max keeps ~0.0003 ETH for gas')
    fireEvent.click(screen.getByRole('button', { name: 'Max' }))
    expect(screen.getByRole('textbox', { name: 'Amount' })).toHaveValue('0.9997')
    fireEvent.click(screen.getByRole('button', { name: '100%' }))
    expect(screen.getByRole('textbox', { name: 'Amount' })).toHaveValue('0.9997')
    fireEvent.click(screen.getByRole('button', { name: '50%' }))
    expect(screen.getByRole('textbox', { name: 'Amount' })).toHaveValue('0.49985')
  })

  it('keeps nothing back when the pay token is not the gas coin', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'wallet.balances'
        ? {
            balances: [
              {
                chainId: 8453,
                token: USDC,
                raw: '12500000',
                amount: '12.5',
                priceUsd: 1,
                valueUsd: 12.5,
                change24hPct: 0,
              },
            ],
          }
        : {},
    )
    mount({ prefill: { chainId: 8453, tokenIn: USDC, tokenOut: ETH, seq: 2 } })
    await screen.findByText(/Balance/)
    expect(screen.queryByTestId('gas-reserve')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Max' }))
    expect(screen.getByRole('textbox', { name: 'Amount' })).toHaveValue('12.5')
  })

  it('refuses more than the wallet holds', async () => {
    mount()
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '5' } })
    await waitFor(() => expect(screen.getByTestId('swap-review')).toHaveTextContent('Not enough'))
    expect(screen.getByTestId('swap-review')).toBeDisabled()
  })

  it('reports what the engine would do with an agent swap of this size', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'trading.quote'
        ? quote({
            valueUsd: 2000,
            guard: {
              decision: 'needs_approval',
              spentTodayUsd: 0,
              dailyCapUsd: 1000,
              thresholdUsd: 100,
            },
          })
        : method === 'wallet.balances'
          ? {
              balances: [
                {
                  chainId: 8453,
                  token: ETH,
                  raw: '1',
                  amount: '10',
                  priceUsd: 2500,
                  valueUsd: 25000,
                  change24hPct: 0,
                },
              ],
            }
          : {},
    )
    mount()
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '0.8' } })
    await waitFor(() =>
      expect(screen.getByTestId('quote-guard')).toHaveAttribute('data-decision', 'needs_approval'),
    )
    expect(screen.getByTestId('quote-guard')).toHaveTextContent('you are confirming it yourself')
  })

  it('opens the confirm sheet from Review and asks the amount to be retyped above 1,000 USD', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'trading.quote'
        ? quote({ valueUsd: 2500, amountIn: '1', amountOut: '2500' })
        : method === 'wallet.balances'
          ? {
              balances: [
                {
                  chainId: 8453,
                  token: ETH,
                  raw: '1',
                  amount: '10',
                  priceUsd: 2500,
                  valueUsd: 25000,
                  change24hPct: 0,
                },
              ],
            }
          : method === 'trading.swap'
            ? { orders: [{ orderId: 'o9', status: 'submitted', txHash: '0xabc' }] }
            : {},
    )
    const onSent = vi.fn()
    mount({ onSent })
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '1' } })
    await waitFor(() => expect(screen.getByTestId('swap-review')).not.toBeDisabled())
    fireEvent.click(screen.getByTestId('swap-review'))

    const send = await screen.findByTestId('confirm-send')
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByTestId('confirm-retype'), { target: { value: '1.0' } })
    expect(send).not.toBeDisabled()
    fireEvent.click(send)

    await waitFor(() => expect(onSent).toHaveBeenCalled())
    const swapCall = rpcCall.mock.calls.find((c) => c[0] === 'trading.swap')
    expect(swapCall?.[1]).toMatchObject({
      chainId: 8453,
      wallets: [WALLET.address],
      tokenIn: ETH.address,
      tokenOut: USDC.address,
      amountIn: '1',
      initiator: 'manual',
    })
  })

  it('cannot review without a Uniswap key or an unlocked vault', () => {
    mount({ providerReady: false })
    expect(screen.getByTestId('swap-review')).toHaveTextContent('Add a Uniswap key')
    expect(rpcCall.mock.calls.some((c) => c[0] === 'trading.quote')).toBe(false)
  })

  it('opens Settings from the no-key button', () => {
    const onOpenSettings = vi.fn()
    mount({ provider: 'uniswap', providerReady: false, onOpenSettings })
    const cta = screen.getByTestId('swap-review')
    expect(cta).toHaveTextContent('Add a Uniswap key')
    expect(cta).not.toBeDisabled()
    fireEvent.click(cta)
    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })

  it('keeps the vault gate closed: a locked vault is not something the ticket can open', () => {
    mount({ unlocked: false })
    const cta = screen.getByTestId('swap-review')
    expect(cta).toHaveTextContent('Unlock the vault')
    expect(cta).toBeDisabled()
  })

  it('hides the previous price while a new amount is being quoted', async () => {
    let release: (() => void) | null = null
    rpcCall.mockImplementation(async (method: string, params?: { amountIn?: string }) => {
      if (method === 'wallet.balances')
        return {
          balances: [
            {
              chainId: 8453,
              token: ETH,
              raw: '1',
              amount: '1',
              priceUsd: 2500,
              valueUsd: 2500,
              change24hPct: 0,
            },
          ],
        }
      if (method === 'trading.quote') {
        if (params?.amountIn === '0.2') {
          await new Promise<void>((resolve) => {
            release = resolve
          })
          return quote({ amountIn: '0.2', amountOut: '500.24', minOut: '497.74' })
        }
        return quote()
      }
      return {}
    })
    mount()
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '0.1' } })
    await waitFor(() => expect(screen.getByTestId('quote-out')).toHaveTextContent('250.12'))
    expect(screen.getByTestId('swap-review')).not.toBeDisabled()

    fireEvent.change(screen.getByRole('textbox', { name: 'Amount' }), {
      target: { value: '0.2' },
    })
    await waitFor(() => expect(release).not.toBeNull())
    // The old price is about another swap: not shown, not reviewable.
    expect(screen.getByTestId('quote-out')).toHaveTextContent('…')
    expect(screen.getByTestId('quote-out')).not.toHaveTextContent('250.12')
    expect(screen.getByTestId('quote-out')).toHaveAttribute('data-pending', 'true')
    expect(screen.queryByTestId('quote-facts')).toBeNull()
    expect(screen.queryByTestId('quote-guard')).toBeNull()
    expect(screen.getByTestId('swap-review')).toBeDisabled()

    await act(async () => {
      release?.()
      await Promise.resolve()
    })
    await waitFor(() => expect(screen.getByTestId('quote-out')).toHaveTextContent('500.24'))
    expect(screen.getByTestId('quote-facts')).toHaveTextContent('497.74 USDC')
    expect(screen.getByTestId('swap-review')).not.toBeDisabled()
  })

  it('drops a wallet override once that wallet is gone', () => {
    const second = {
      ...WALLET,
      address: '0x2222222222222222222222222222222222222222',
      label: 'Second',
      primary: false,
    }
    const view = (wallets: (typeof WALLET)[]) => (
      <SwapPanel
        wallets={wallets}
        primary={WALLET.address}
        selectedWallet="all"
        provider="uniswap"
        providerReady
        onOpenSettings={vi.fn()}
        unlocked
        prefill={null}
        onSent={vi.fn()}
      />
    )
    const { rerender } = renderDesk(view([WALLET, second]))
    const select = screen.getByLabelText('From wallet')
    fireEvent.change(select, { target: { value: second.address } })
    expect(select).toHaveValue(second.address)
    rerender(view([WALLET]))
    expect(screen.getByLabelText('From wallet')).toHaveValue(WALLET.address)
  })

  it('shows the route error the gateway returns', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.quote') throw new Error('trading.no_route: No route for this pair')
      if (method === 'wallet.balances')
        return {
          balances: [
            {
              chainId: 8453,
              token: ETH,
              raw: '1',
              amount: '1',
              priceUsd: 2500,
              valueUsd: 2500,
              change24hPct: 0,
            },
          ],
        }
      return {}
    })
    mount()
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '0.1' } })
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('No route for this pair'),
    )
    expect(screen.getByTestId('swap-review')).toBeDisabled()
  })
})

describe('SwapPanel · the gas coin logo', () => {
  const payLogo = () => screen.getAllByTestId('token-button')[0]?.querySelector('img')

  it('shows the ETH logo on a fresh ticket, before anything is picked', () => {
    mount({ prefill: null })
    expect(payLogo()?.getAttribute('src')).toContain('ethereum.png')
  })

  it('keeps the ETH logo when the ticket moves between chains', () => {
    mount({ prefill: null })
    fireEvent.click(screen.getByRole('radio', { name: 'Robinhood' }))
    expect(payLogo()?.getAttribute('src')).toContain('ethereum.png')
    fireEvent.click(screen.getByRole('radio', { name: 'Base' }))
    expect(payLogo()?.getAttribute('src')).toContain('ethereum.png')
  })
})

describe('SwapPanel · providers', () => {
  it('names the provider on the quote line', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'trading.quote'
        ? quote({ provider: 'aggregator' })
        : method === 'wallet.balances'
          ? {
              balances: [
                {
                  chainId: 8453,
                  token: ETH,
                  raw: '1',
                  amount: '1',
                  priceUsd: 2500,
                  valueUsd: 2500,
                  change24hPct: 0,
                },
              ],
            }
          : {},
    )
    mount({ provider: 'aggregator' })
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '0.1' } })
    await waitFor(() =>
      expect(screen.getByTestId('quote-provider')).toHaveTextContent('via AgentOS Aggregator'),
    )
  })

  it('shows what the provider refused instead of a dead ticket', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'trading.quote') {
        const err = new Error('TSLA is not authorized for trade.') as Error & { code?: string }
        err.code = 'trading.token_not_tradeable'
        throw err
      }
      if (method === 'wallet.balances')
        return {
          balances: [
            {
              chainId: 8453,
              token: ETH,
              raw: '1',
              amount: '1',
              priceUsd: 2500,
              valueUsd: 2500,
              change24hPct: 0,
            },
          ],
        }
      return {}
    })
    mount({ provider: 'aggregator' })
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '0.1' } })
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('not authorized for trade'),
    )
    expect(screen.getByTestId('swap-review')).toBeDisabled()
  })

  it('does not ask for a key when the aggregator is the provider', () => {
    mount({ provider: 'aggregator', providerReady: true })
    expect(screen.getByTestId('swap-review')).toHaveTextContent('Enter an amount')
  })
})

describe('ConfirmSwap', () => {
  it('says why a refresh failed instead of leaving a dead button', () => {
    const sheet = (quoteError: string | null) => (
      <ConfirmSwap
        quote={quote()}
        fetchedAt={0}
        wallet={WALLET}
        tokenIn={ETH}
        tokenOut={USDC}
        amount="0.1"
        slippagePct={undefined}
        refreshing={false}
        quoteError={quoteError}
        onRefresh={vi.fn()}
        onClose={vi.fn()}
        onSent={vi.fn()}
      />
    )
    const { rerender } = renderDesk(sheet(null))
    expect(screen.getByTestId('confirm-stale')).toBeInTheDocument()
    expect(screen.queryByTestId('confirm-quote-error')).toBeNull()
    rerender(sheet('trading.no_route: No route for this pair'))
    expect(screen.getByTestId('confirm-quote-error')).toHaveTextContent(
      'Could not refresh the price: trading.no_route: No route for this pair',
    )
    expect(screen.getByTestId('confirm-refresh')).toBeInTheDocument()
  })
})

describe('SwapPanel · provider warnings', () => {
  it('lists what the provider flagged, in the ticket and again in the confirm sheet', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'trading.quote'
        ? quote({ provider: 'aggregator', warnings: ['USDC charges a 1% fee on transfer.', ''] })
        : method === 'wallet.balances'
          ? {
              balances: [
                {
                  chainId: 8453,
                  token: ETH,
                  raw: '1',
                  amount: '1',
                  priceUsd: 2500,
                  valueUsd: 2500,
                  change24hPct: 0,
                },
              ],
            }
          : {},
    )
    mount({ provider: 'aggregator' })
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '0.1' } })
    const list = await screen.findByTestId('quote-warnings')
    expect(list.querySelectorAll('li')).toHaveLength(1)
    expect(list).toHaveTextContent('USDC charges a 1% fee on transfer.')
    fireEvent.click(screen.getByTestId('swap-review'))
    await screen.findByTestId('confirm-send')
    expect(screen.getAllByTestId('quote-warnings')).toHaveLength(2)
  })
})

describe('SwapPanel · wallet picker', () => {
  it('lists the active wallet first and marks it with a check, not a star', () => {
    const second = {
      ...WALLET,
      address: '0x2222222222222222222222222222222222222222',
      label: 'Second',
      primary: false,
    }
    mount({ wallets: [second, WALLET] })
    const options = within(screen.getByLabelText('From wallet')).getAllByRole('option')
    expect(options.map((o) => o.textContent)).toEqual([`${WALLET.label} ✓`, second.label])
  })
})
