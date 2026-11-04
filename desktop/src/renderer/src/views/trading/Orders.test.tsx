import { readFileSync } from 'node:fs'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Orders } from './Orders'
import { formatAmount } from './logic'
import { bridgeOrder, order, renderDesk, USDC } from './test-utils'
import type { Order } from './types'

const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
const openExternal = vi.fn(async () => {})
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal } }),
  isDesktop: () => true,
}))

beforeEach(() => {
  rpcCall.mockReset()
  rpcCall.mockResolvedValue({})
  openExternal.mockClear()
})

describe('Orders · approvals', () => {
  it('lists what the agent is waiting on, with a timer, and approves it', () => {
    const onDecide = vi.fn()
    const waiting = order()
    renderDesk(
      <Orders
        orders={[
          waiting,
          order({
            orderId: 'o2',
            status: 'confirmed',
            txHash: '0xabc',
            explorerUrl: 'https://basescan.org/tx/0xabc',
          }),
        ]}
        approvalsOnly
        deciding={null}
        onDecide={onDecide}
        showWallet={false}
        highlight={null}
      />,
    )
    const rows = screen.getAllByTestId('order-row')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toHaveTextContent('0.2 ETH')
    expect(rows[0]).toHaveTextContent('USDC')
    expect(rows[0]).toHaveTextContent('Needs approval')
    expect(rows[0]).toHaveTextContent(/expires in 9:5\d|expires in 10:00/)
    expect(rows[0]).toHaveTextContent('Agent')
    expect(rows[0]).toHaveTextContent('Note: DCA')

    // $500 sits on the chat card's high-risk line: Approve arms first here too.
    const approve = screen.getByTestId('order-approve')
    fireEvent.click(approve)
    expect(approve).toHaveTextContent('Click again to approve')
    expect(onDecide).not.toHaveBeenCalled()
    fireEvent.click(approve)
    expect(onDecide).toHaveBeenCalledWith(waiting, true)
    fireEvent.click(screen.getByTestId('order-reject'))
    expect(onDecide).toHaveBeenCalledWith(waiting, false)
  })

  it('approves a small order in one click, and lets an armed one relax', () => {
    vi.useFakeTimers()
    const onDecide = vi.fn()
    const small = order({ orderId: 's1', valueUsd: 40 })
    const big = order({ orderId: 'b1', valueUsd: 5000 })
    renderDesk(
      <Orders
        orders={[small, big]}
        approvalsOnly
        deciding={null}
        onDecide={onDecide}
        showWallet={false}
        highlight={null}
      />,
    )
    const [approveSmall, approveBig] = screen.getAllByTestId('order-approve')
    fireEvent.click(approveSmall!)
    expect(onDecide).toHaveBeenCalledWith(small, true)
    fireEvent.click(approveBig!)
    expect(approveBig).toHaveAttribute('data-armed', 'true')
    act(() => {
      vi.advanceTimersByTime(4100)
    })
    expect(approveBig).not.toHaveAttribute('data-armed')
    expect(approveBig).toHaveTextContent('Approve')
    expect(onDecide).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('says the list could not be read, with a retry, instead of "no orders"', () => {
    const onRetry = vi.fn()
    renderDesk(
      <Orders
        orders={[]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
        error={new Error('socket closed')}
        onRetry={onRetry}
      />,
    )
    expect(screen.queryByText('No orders yet')).toBeNull()
    expect(screen.getByText('socket closed')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('trading-error-retry'))
    expect(onRetry).toHaveBeenCalled()
  })

  it('carries the trade, its value and the quiet facts on one row', () => {
    renderDesk(
      <Orders
        orders={[
          order({
            orderId: 'c2',
            status: 'confirmed',
            initiator: 'manual',
            note: null,
            expiresAt: null,
            priceImpactPct: 4.2,
          }),
        ]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    const row = screen.getByTestId('order-row')
    expect(row).toHaveTextContent('0.2 ETH')
    expect(row).toHaveTextContent('500 USDC')
    expect(row).toHaveTextContent('Confirmed')
    expect(row).toHaveTextContent('You')
    // The row's own value column, then the caption's short-labelled figures.
    expect(row).toHaveTextContent('$500.00')
    expect([...row.querySelectorAll('.trd-order__fact')].map((el) => el.textContent)).toEqual(
      expect.arrayContaining(['min497', 'vs ref4.20%', 'fee$0.04']),
    )
    // A heavy impact is toned rather than left in the caption's grey.
    expect(row.querySelector('b[data-tone="warn"]')).toHaveTextContent('4.20%')
    // Nothing waits, so no timer and no decision buttons.
    expect(screen.queryByTestId('order-approve')).toBeNull()
  })

  it('names the trigger a fired swap belongs to, with its note and its id on hover', () => {
    renderDesk(
      <Orders
        orders={[
          order({
            orderId: 't1',
            status: 'confirmed',
            note: 'Stop-loss ETH · fired at $3,790',
            triggerId: 'trg_1a2b3c4d',
            expiresAt: null,
          }),
        ]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    const row = screen.getByTestId('order-row')
    expect(row).toHaveTextContent('Stop-loss ETH · fired at $3,790')
    const chip = screen.getByTestId('order-trigger')
    expect(chip).toHaveTextContent('Trigger')
    expect(chip).toHaveAttribute('title', 'trg_1a2b3c4d')
    expect(screen.queryByTestId('order-dca')).toBeNull()
  })

  it('shows what the receipt delivered once confirmed, and the estimate — marked — before', () => {
    renderDesk(
      <Orders
        orders={[
          order({
            orderId: 'done',
            status: 'confirmed',
            expectedOut: '500',
            receivedOut: '498.6',
            expiresAt: null,
          }),
          order({ orderId: 'sent', status: 'submitted', expectedOut: '500', receivedOut: null }),
          order({ orderId: 'old', status: 'confirmed', expectedOut: '500', expiresAt: null }),
        ]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    const [done, sent, old] = screen.getAllByTestId('order-row')
    // Confirmed: the receipt's figure, no "est.", and the fee is the fee.
    expect(done).toHaveTextContent('498.6 USDC')
    expect(done).not.toHaveTextContent('500 USDC')
    expect(done).not.toHaveTextContent('est.')
    expect(done!.querySelector('.trd-order__leg[data-estimate]')).toBeNull()
    expect([...done!.querySelectorAll('.trd-order__fact')].map((el) => el.textContent)).toContain(
      'fee$0.04',
    )
    // Still in flight: the quote's guess, said to be one, and an estimated fee.
    expect(sent).toHaveTextContent('est. 500 USDC')
    expect(sent!.querySelector('.trd-order__leg[data-estimate="true"]')).not.toBeNull()
    expect([...sent!.querySelectorAll('.trd-order__fact')].map((el) => el.textContent)).toContain(
      'est. fee$0.04',
    )
    // An older engine's receipt with no receivedOut: the estimate, unmarked.
    expect(old).toHaveTextContent('500 USDC')
    expect(old).not.toHaveTextContent('est.')
  })

  it('locks the buttons while a decision is in flight', () => {
    renderDesk(
      <Orders
        orders={[order()]}
        approvalsOnly
        deciding="o1"
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    expect(screen.getByTestId('order-approve')).toBeDisabled()
    expect(screen.getByTestId('order-reject')).toBeDisabled()
  })

  it('says why a settled order did not go through, and opens the explorer for one that did', () => {
    renderDesk(
      <Orders
        orders={[
          order({ orderId: 'f1', status: 'failed', reason: 'insufficient gas', expiresAt: null }),
          order({
            orderId: 'c1',
            status: 'confirmed',
            txHash: '0xabc',
            explorerUrl: 'https://basescan.org/tx/0xabc',
            expiresAt: null,
          }),
        ]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet
        highlight={null}
      />,
    )
    const rows = screen.getAllByTestId('order-row')
    expect(rows[0]).toHaveTextContent('Reason: insufficient gas')
    expect(rows[0]).toHaveTextContent('0x1111…1111')
    fireEvent.click(screen.getByRole('button', { name: 'View transaction' }))
    expect(openExternal).toHaveBeenCalledWith('https://basescan.org/tx/0xabc')
  })

  it('does not offer to unwrap when the delivered token is the one asked for', () => {
    renderDesk(
      <Orders
        orders={[
          order({
            orderId: 'u1',
            status: 'confirmed',
            tokenIn: { ...USDC, symbol: 'ETH', native: true, address: '0x0' },
            tokenOut: USDC,
            deliveredToken: USDC,
            expiresAt: null,
          }),
        ]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    expect(screen.queryByTestId('order-delivered')).toBeNull()
  })

  it('offers to unwrap when a swap to ETH delivered WETH', async () => {
    rpcCall.mockImplementation(async (method: string) =>
      method === 'trading.unwrap' ? { txHash: '0xdef' } : {},
    )
    renderDesk(
      <Orders
        orders={[
          order({
            orderId: 'w1',
            status: 'confirmed',
            tokenIn: USDC,
            deliveredToken: { ...USDC, symbol: 'WETH', name: 'Wrapped Ether', decimals: 18 },
            expiresAt: null,
          }),
        ]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    expect(screen.getByTestId('order-delivered')).toHaveTextContent('Received WETH instead of ETH.')
    fireEvent.click(screen.getByTestId('unwrap'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('trading.unwrap', {
        chainId: 8453,
        wallet: order().wallet,
      }),
    )
  })

  it('scrolls a highlighted row into view once, then hands the highlight back', () => {
    vi.useFakeTimers()
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    const onHighlighted = vi.fn()
    const view = (highlight: string | null) => (
      <Orders
        orders={[order({ orderId: 'h1' }), order({ orderId: 'h2' })]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={highlight}
        onHighlighted={onHighlighted}
      />
    )
    const { rerender } = renderDesk(view('h2'))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    expect(onHighlighted).toHaveBeenCalledTimes(1)
    // The approval timers re-render the rows every second; no more scrolling.
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    rerender(view('h2'))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    // Highlight cleared by the owner: still nothing.
    rerender(view(null))
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('shows the empty invitation when nothing waits', () => {
    renderDesk(
      <Orders
        orders={[]}
        approvalsOnly
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    expect(screen.getByText('Nothing waiting')).toBeInTheDocument()
  })
})

describe('Orders · bridge rows', () => {
  function row(o: Order): HTMLElement {
    renderDesk(
      <Orders
        orders={[o]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    return screen.getByTestId('order-row')
  }

  it('a parked bridge names the destination chain and the estimate, with no fill pill', () => {
    const el = row(bridgeOrder())
    expect(el).toHaveTextContent('0.01 ETH')
    const to = screen.getByTestId('order-bridge-to')
    expect(to).toHaveTextContent('Robinhood Chain')
    expect(to).toHaveTextContent('est. 0.0099 ETH')
    expect(to).toHaveAttribute('data-estimate', 'true')
    expect(el).toHaveTextContent('Needs approval')
    // The Relay solver address is the deposit target, not a recipient to read.
    expect(el).not.toHaveTextContent('0xa5f5')
    expect(screen.queryByTestId('order-bridge-fill')).toBeNull()
  })

  it('a submitted bridge wears Relay’s fill status until the funds land', () => {
    row(
      bridgeOrder({
        status: 'submitted',
        txHash: '0xdep',
        explorerUrl: 'https://basescan.org/tx/0xdep',
        bridge: { ...bridgeOrder().bridge!, requestId: 'req_1', fillStatus: 'pending' },
      }),
    )
    const pill = screen.getByTestId('order-bridge-fill')
    expect(pill).toHaveTextContent('filling')
    expect(pill).toHaveAttribute('data-fill', 'pending')
    expect(pill).toHaveAttribute('title', 'req_1')
  })

  it('a confirmed bridge shows what arrived, without the estimate mark', () => {
    row(
      bridgeOrder({
        status: 'confirmed',
        txHash: '0xdep',
        receivedOut: '0.00985',
        bridge: { ...bridgeOrder().bridge!, fillStatus: 'success' },
      }),
    )
    const to = screen.getByTestId('order-bridge-to')
    expect(to).toHaveTextContent('0.00985 ETH')
    expect(to).not.toHaveTextContent('est.')
    expect(to).not.toHaveAttribute('data-estimate')
    expect(screen.queryByTestId('order-bridge-fill')).toBeNull()
  })
})

describe('Orders · LP rows', () => {
  const FIXTURES = 'src/renderer/src/views/trading/desk/__fixtures__/lp'
  const lp = (kind: 'lp_add' | 'lp_remove', extra: Partial<Order> = {}): Order => ({
    ...(JSON.parse(readFileSync(`${FIXTURES}/${kind}.json`, 'utf8')) as Order),
    expiresAt: Date.now() + 600_000,
    ...extra,
  })

  function row(o: Order): HTMLElement {
    renderDesk(
      <Orders
        orders={[o]}
        approvalsOnly={false}
        deciding={null}
        onDecide={vi.fn()}
        showWallet={false}
        highlight={null}
      />,
    )
    return screen.getByTestId('order-row')
  }

  it('an add shows the deposit it plans, with its dollars — never amountIn (the liquidity)', () => {
    const add = lp('lp_add')
    const el = row(add)
    expect(el).toHaveTextContent('Add liquidity')
    const amounts = screen.getByTestId('order-lp-amounts')
    expect(amounts).toHaveTextContent('deposit 0.020404 WETH · $50.00')
    expect(amounts).toHaveAttribute('data-estimate', 'true')
    // The raw liquidity the engine files under amountIn is nowhere on the row.
    expect(el).not.toHaveTextContent(formatAmount(add.amountIn))
    expect(el).not.toHaveTextContent('0.0000281172')
    // The dollars are said once, beside the deposit, not again in the headline.
    expect(el.querySelector('.trd-order__head')?.textContent?.match(/\$50\.00/g)).toHaveLength(1)
  })

  it('a confirmed add shows what the receipt took', () => {
    const add = lp('lp_add', {
      status: 'confirmed',
      spent: {
        base: { raw: '0', human: '0', usd: 0 },
        quote: { raw: '20391000000000000', human: '0.020391', usd: 49.97 },
      },
    })
    row(add)
    const amounts = screen.getByTestId('order-lp-amounts')
    expect(amounts).toHaveTextContent('deposit 0.020391 WETH')
    expect(amounts).not.toHaveAttribute('data-estimate')
  })

  it('a remove shows what comes back', () => {
    row(lp('lp_remove'))
    expect(screen.getByTestId('order-lp-amounts')).toHaveTextContent(/^receive /)
  })
})
