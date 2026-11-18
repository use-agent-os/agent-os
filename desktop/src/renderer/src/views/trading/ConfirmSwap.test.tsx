import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ConfirmSwap } from './ConfirmSwap'
import { ETH, quote, renderDesk, USDC, WALLET } from './test-utils'
import type { Quote } from './types'

const rpcCall = vi.fn<(...args: unknown[]) => Promise<unknown>>(async () => ({ orders: [] }))
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
const toastFn = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: toastFn }))

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockResolvedValue({ orders: [] })
  toastFn.success.mockClear()
  toastFn.error.mockClear()
  toastFn.warning.mockClear()
})

describe('ConfirmSwap · the engine is held to the frozen numbers', () => {
  const sheet = (q: Quote, fetchedAt: number, onRefresh = vi.fn()) => (
    <ConfirmSwap
      quote={q}
      fetchedAt={fetchedAt}
      wallet={WALLET}
      tokenIn={ETH}
      tokenOut={USDC}
      amount="0.1"
      slippagePct={undefined}
      refreshing={false}
      onRefresh={onRefresh}
      onClose={vi.fn()}
      onSent={vi.fn()}
    />
  )

  it('sends the frozen quote’s raw figures and id, not the live re-quote’s', async () => {
    const now = Date.now()
    const frozen = quote({
      quoteId: 'q-frozen',
      amountOut: '250.12',
      amountOutRaw: '250120000',
      minOut: '248.87',
      minOutRaw: '248870000',
    })
    const { rerender } = renderDesk(sheet(frozen, now))
    // The panel behind re-quotes; the sheet did not ask, so it keeps its numbers.
    rerender(
      sheet(
        quote({
          quoteId: 'q-live',
          amountOut: '199',
          amountOutRaw: '199000000',
          minOut: '198',
          minOutRaw: '198000000',
        }),
        now + 2_000,
      ),
    )
    fireEvent.click(screen.getByTestId('confirm-send'))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledTimes(1))
    expect(rpcCall).toHaveBeenCalledWith(
      'trading.swap',
      expect.objectContaining({
        expectedOutRaw: '250120000',
        minOutRaw: '248870000',
        quoteId: 'q-frozen',
        amountIn: '0.1',
        initiator: 'manual',
      }),
    )
  })

  it('lists the AgentOS DEX fee in the sell token', () => {
    const fee = { bps: 20, pct: 0.2, symbol: 'ETH', amount: '0.0002', amountRaw: '200000000000000' }
    renderDesk(sheet(quote({ swapFee: fee }), Date.now()))
    const row = screen.getByTestId('quote-dex-fee')
    expect(row).toHaveTextContent('AgentOS DEX Fee')
    expect(row).toHaveTextContent('0.0002 ETH')
  })

  it('derives the raw figures from the decimals when an older engine sends none', async () => {
    const q = quote({ quoteId: 'q2', amountOut: '250.12', minOut: '248.87' })
    delete (q as Partial<Quote>).amountOutRaw
    delete (q as Partial<Quote>).minOutRaw
    renderDesk(sheet(q, Date.now()))
    fireEvent.click(screen.getByTestId('confirm-send'))
    await waitFor(() => expect(rpcCall).toHaveBeenCalledTimes(1))
    // USDC has 6 decimals.
    expect(rpcCall).toHaveBeenCalledWith(
      'trading.swap',
      expect.objectContaining({ expectedOutRaw: '250120000', minOutRaw: '248870000' }),
    )
  })

  it('re-quotes instead of failing when the engine says the price moved', async () => {
    const moved = Object.assign(new Error('price moved'), { code: 'trading.price_moved' })
    rpcCall.mockRejectedValueOnce(moved)
    const onRefresh = vi.fn()
    renderDesk(sheet(quote(), Date.now(), onRefresh))
    fireEvent.click(screen.getByTestId('confirm-send'))
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1))
    expect(toastFn.warning).toHaveBeenCalledWith(
      'Price moved since you confirmed — re-quoting',
      expect.anything(),
    )
    expect(toastFn.error).not.toHaveBeenCalled()
    // The sheet is still open, on the same numbers, until the next quote lands.
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('still reports any other failure as an error and does not re-quote', async () => {
    rpcCall.mockRejectedValueOnce(Object.assign(new Error('nope'), { code: 'trading.invalid' }))
    const onRefresh = vi.fn()
    renderDesk(sheet(quote(), Date.now(), onRefresh))
    fireEvent.click(screen.getByTestId('confirm-send'))
    await waitFor(() => expect(toastFn.error).toHaveBeenCalled())
    expect(onRefresh).not.toHaveBeenCalled()
  })

  it("counts the frozen quote down to the engine's expiresAt, not a fixed 15 s", () => {
    const now = Date.now()
    // Fetched 20 s ago, but the engine honours it for 10 s more: not stale.
    renderDesk(sheet(quote({ expiresAt: now + 10_000 }), now - 20_000))
    expect(screen.queryByTestId('confirm-stale')).toBeNull()
    expect(screen.getByTestId('confirm-send')).toBeInTheDocument()
  })

  it('is stale the moment the engine stops honouring the price', () => {
    const now = Date.now()
    // Fetched 5 s ago, but the engine only gave it 3 s: stale already.
    renderDesk(sheet(quote({ expiresAt: now - 2_000 }), now - 5_000))
    expect(screen.getByTestId('confirm-stale')).toBeInTheDocument()
  })
})

describe('ConfirmSwap · the quote is frozen while the sheet is open', () => {
  const sheet = (
    amountOut: string,
    fetchedAt: number,
    refreshing: boolean,
    onRefresh = vi.fn(),
  ) => (
    <ConfirmSwap
      quote={quote({ amountOut, minOut: amountOut, expiresAt: fetchedAt + 15_000 })}
      fetchedAt={fetchedAt}
      wallet={WALLET}
      tokenIn={ETH}
      tokenOut={USDC}
      amount="0.1"
      slippagePct={undefined}
      refreshing={refreshing}
      onRefresh={onRefresh}
      onClose={vi.fn()}
      onSent={vi.fn()}
    />
  )

  it('keeps the numbers it opened with while the panel behind re-quotes', () => {
    const now = Date.now()
    const { rerender } = renderDesk(sheet('250.12', now, false))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('250.12')
    rerender(sheet('199.00', now + 15_000, false))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('250.12')
    expect(screen.getByRole('alertdialog')).not.toHaveTextContent('199')
    expect(screen.queryByTestId('confirm-stale')).toBeNull()
  })

  it('adopts the next quote only after Refresh quote was asked for', () => {
    const now = Date.now()
    const onRefresh = vi.fn()
    const { rerender } = renderDesk(sheet('250.12', now, false, onRefresh))
    fireEvent.click(screen.getByTestId('confirm-refresh-quote'))
    expect(onRefresh).toHaveBeenCalledTimes(1)
    // Still fetching: the old numbers stay on screen.
    rerender(sheet('250.12', now, true, onRefresh))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('250.12')
    rerender(sheet('199.00', now + 3_000, false, onRefresh))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('199')
    expect(screen.getByRole('alertdialog')).not.toHaveTextContent('250.12')
  })

  it('still counts the frozen quote down and refreshes through the stale button', () => {
    const onRefresh = vi.fn()
    const { rerender } = renderDesk(sheet('250.12', 0, false, onRefresh))
    expect(screen.getByTestId('confirm-stale')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('confirm-refresh'))
    expect(onRefresh).toHaveBeenCalledTimes(1)
    rerender(sheet('251.00', Date.now(), false, onRefresh))
    expect(screen.queryByTestId('confirm-stale')).toBeNull()
    expect(screen.getByTestId('confirm-send')).toBeInTheDocument()
    expect(screen.getByRole('alertdialog')).toHaveTextContent('251')
  })
})
