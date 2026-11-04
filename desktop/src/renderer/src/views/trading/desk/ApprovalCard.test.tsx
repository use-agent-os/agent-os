import { readFileSync } from 'node:fs'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthResult } from '@shared/app'
import { DEFAULT_SETTINGS, type TouchIdMode } from '@shared/settings'
import { resetBiometricGateForTests, useTouchIdPrompt } from '~/lib/biometric-gate'
import { useSettings } from '~/stores/settings'
import { useOrderDecision } from '~/stores/trading'
import { bridgeOrder, order, renderDesk, USDC, WALLET } from '../test-utils'
import type { Order } from '../types'
import { ApprovalCard } from './ApprovalCard'
import { ApprovalsRegion } from './ApprovalsRegion'

const openExternal = vi.fn(async () => {})
const authenticate = vi.fn<(reason: string) => Promise<AuthResult>>()
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal, authenticate } }),
  isDesktop: () => true,
}))
const rpcCall = vi.fn()
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn(), dismiss: vi.fn() },
}))

function setTouchId(touchId: TouchIdMode): void {
  useSettings.setState({
    loaded: true,
    settings: { ...structuredClone(DEFAULT_SETTINGS), security: { touchId } },
  })
}

beforeEach(() => {
  openExternal.mockClear()
  authenticate.mockReset()
  rpcCall.mockReset()
  rpcCall.mockResolvedValue({ order: {} })
  resetBiometricGateForTests()
  setTouchId('off')
})

describe('ApprovalCard', () => {
  it('shows bound facts, focuses Reject first, and approves a normal order in one click', () => {
    const onApprove = vi.fn()
    const onReject = vi.fn()
    renderDesk(
      <ApprovalCard
        order={order({ valueUsd: 120, priceImpactPct: 0.3, note: 'DCA leg 3' })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={onApprove}
        onReject={onReject}
        focusOnMount
      />,
    )
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('Approval needed · Swap')
    expect(card).toHaveTextContent('Main · 0x1111…1111')
    expect(card).toHaveTextContent('0.2 ETH')
    expect(card).toHaveTextContent('500 USDC')
    expect(card).toHaveTextContent('DCA leg 3')
    expect(card).toHaveTextContent(/UTC[+−]/)
    expect(screen.queryByTestId('risk-high')).toBeNull()
    expect(document.activeElement).toBe(screen.getByTestId('card-reject'))
    fireEvent.click(screen.getByTestId('card-approve'))
    expect(onApprove).toHaveBeenCalledTimes(1)
  })

  it('arms a high-risk approve and executes on the second click within the window', () => {
    vi.useFakeTimers()
    const onApprove = vi.fn()
    renderDesk(
      <ApprovalCard
        order={order({ valueUsd: 900 })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={onApprove}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    expect(screen.getByTestId('risk-high')).toBeInTheDocument()
    const approve = screen.getByTestId('card-approve')
    fireEvent.click(approve)
    expect(onApprove).not.toHaveBeenCalled()
    expect(approve).toHaveTextContent('Click again to approve and execute')
    act(() => {
      vi.advanceTimersByTime(4100)
    })
    expect(approve).toHaveTextContent('Approve')
    fireEvent.click(approve)
    fireEvent.click(approve)
    expect(onApprove).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('says why the engine is asking while the card is still live', () => {
    renderDesk(
      <ApprovalCard
        order={order({ reason: 'price moved 2.4% since the quote' })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    // Live: the buttons are there, and so is the reason — it is what decides
    // the decision, so it may not wait for the outcome to be shown.
    expect(screen.getByTestId('card-approve')).toBeInTheDocument()
    expect(screen.getByTestId('card-reason')).toHaveTextContent('price moved 2.4% since the quote')
    // The impact fact wears the honest label: a quote against a reference, not a measured move.
    expect(screen.getByTestId('approval-card')).toHaveTextContent('Price vs reference')
    expect(screen.getByTestId('approval-card')).not.toHaveTextContent('Price impact')
  })

  it('rejects with a note the agent will read', () => {
    const onReject = vi.fn()
    renderDesk(
      <ApprovalCard
        order={order()}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={onReject}
        focusOnMount={false}
      />,
    )
    fireEvent.click(screen.getByTestId('card-reject'))
    const reason = screen.getByTestId('reject-reason')
    fireEvent.change(reason, { target: { value: 'slippage too high' } })
    fireEvent.click(screen.getByTestId('card-reject'))
    expect(onReject).toHaveBeenCalledWith(
      expect.objectContaining({ orderId: 'o1' }),
      'slippage too high',
    )
    // An agent's order (it came from a session): the reason goes back to it.
    expect(reason).toHaveAttribute('placeholder', 'Why? (the agent reads this)')
  })

  it('asks for an optional reason on an order no agent asked for', () => {
    renderDesk(
      <ApprovalCard
        order={order({ sessionKey: null, initiator: 'manual' })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    fireEvent.click(screen.getByTestId('card-reject'))
    const reason = screen.getByTestId('reject-reason')
    expect(reason).toHaveAttribute('placeholder', 'Why? (optional)')
    expect(reason).toHaveAttribute('aria-label', 'Why? (optional)')
    expect(reason).not.toHaveAttribute('placeholder', expect.stringContaining('agent'))
  })

  it('sends the note once on Enter and ignores a second Enter while the decision is in flight', () => {
    const onReject = vi.fn()
    const card = (deciding: boolean) => (
      <ApprovalCard
        order={order()}
        wallets={[WALLET]}
        deciding={deciding}
        onApprove={vi.fn()}
        onReject={onReject}
        focusOnMount={false}
      />
    )
    const { rerender } = renderDesk(card(false))
    fireEvent.click(screen.getByTestId('card-reject'))
    const reason = screen.getByTestId('reject-reason')
    fireEvent.change(reason, { target: { value: 'not now' } })
    fireEvent.keyDown(reason, { key: 'Enter' })
    expect(onReject).toHaveBeenCalledTimes(1)
    // The owner marks the order as deciding; the same key again does nothing.
    rerender(card(true))
    fireEvent.keyDown(screen.getByTestId('reject-reason'), { key: 'Enter' })
    fireEvent.click(screen.getByTestId('card-reject'))
    expect(onReject).toHaveBeenCalledTimes(1)
  })

  it('leaves a settled stamp with the outcome and the tx link', () => {
    renderDesk(
      <ApprovalCard
        order={order({
          status: 'confirmed',
          txHash: '0xabcdef1234',
          explorerUrl: 'https://basescan.org/tx/0xabcdef1234',
        })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    expect(screen.queryByTestId('card-approve')).toBeNull()
    const settled = screen.getByTestId('approval-card')
    expect(settled).toHaveTextContent('Confirmed')
    // An agent swap under the limits settles without ever asking, so the
    // heading must not claim an approval that was never requested.
    expect(settled).not.toHaveTextContent('Approval needed')
    expect(settled.querySelector('.trd-card__title')).toHaveTextContent('Swap')
    // The hash is a real link, as on the ledger row: href, a new tab, no opener.
    const link = screen.getByTestId('card-tx')
    expect(link.tagName).toBe('A')
    expect(link).toHaveTextContent(/0xabcd/)
    expect(link).toHaveAttribute('href', 'https://basescan.org/tx/0xabcdef1234')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('data-tx', 'link')
  })

  it('without an explorer page the settled hash copies, and never pretends to be a link', () => {
    renderDesk(
      <ApprovalCard
        order={order({ status: 'confirmed', txHash: '0xabcdef1234', explorerUrl: null })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const tx = screen.getByTestId('card-tx')
    expect(tx.tagName).toBe('BUTTON')
    expect(tx).not.toHaveAttribute('href')
    expect(tx).toHaveAttribute('data-tx', 'copy')
  })

  it('reads "Reject" until a note is typed, then "Reject with note"', () => {
    const onReject = vi.fn()
    renderDesk(
      <ApprovalCard
        order={order()}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={onReject}
        focusOnMount={false}
      />,
    )
    const reject = screen.getByTestId('card-reject')
    expect(reject).toHaveTextContent(/^Reject$/)
    fireEvent.click(reject)
    // The note field is open but empty: nothing is sent "with a note".
    expect(screen.getByTestId('reject-reason')).toBeInTheDocument()
    expect(reject).toHaveTextContent(/^Reject$/)
    fireEvent.change(screen.getByTestId('reject-reason'), { target: { value: '   ' } })
    expect(reject).toHaveTextContent(/^Reject$/)
    fireEvent.change(screen.getByTestId('reject-reason'), { target: { value: 'too wide' } })
    expect(reject).toHaveTextContent('Reject with note')
    fireEvent.change(screen.getByTestId('reject-reason'), { target: { value: '' } })
    expect(reject).toHaveTextContent(/^Reject$/)
    fireEvent.click(reject)
    expect(onReject).toHaveBeenCalledWith(expect.objectContaining({ orderId: 'o1' }), '')
  })
})

describe('ApprovalsRegion', () => {
  it('renders nothing with no asks, and docks pending cards before settled stamps', () => {
    const { container, rerender } = renderDesk(
      <ApprovalsRegion
        pending={[]}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
      />,
    )
    expect(container.querySelector('[data-testid=approvals-region]')).toBeNull()
    rerender(
      <ApprovalsRegion
        pending={[order({ orderId: 'p1' })]}
        settled={[order({ orderId: 's1', status: 'rejected', reason: 'user' })]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
      />,
    )
    const cards = screen.getAllByTestId('approval-card')
    expect(cards).toHaveLength(2)
    expect(cards[0]).toHaveAttribute('data-order', 'p1')
    expect(screen.getByTestId('approval-stamp')).toHaveTextContent('Rejected')
  })

  it('closes a settled stamp by hand, and never offers that on a pending ask', () => {
    const onDismiss = vi.fn()
    renderDesk(
      <ApprovalsRegion
        pending={[order({ orderId: 'p1' })]}
        settled={[order({ orderId: 's1', status: 'confirmed' })]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
        onDismiss={onDismiss}
      />,
    )
    const dismiss = screen.getAllByTestId('card-dismiss')
    expect(dismiss).toHaveLength(1)
    expect(screen.getByTestId('approval-stamp')).toContainElement(dismiss[0]!)
    fireEvent.click(dismiss[0]!)
    expect(onDismiss).toHaveBeenCalledWith('s1')
  })
})

describe('ApprovalCard for a bridge', () => {
  it('names the destination chain, the expected arrival and the fees, and marks it irreversible', () => {
    renderDesk(
      <ApprovalCard
        order={bridgeOrder()}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveAttribute('data-kind', 'bridge')
    expect(card).toHaveTextContent('Approval needed · Bridge')
    expect(screen.getByTestId('stamp-irreversible')).toBeInTheDocument()
    const legs = screen.getByTestId('card-legs')
    expect(legs).toHaveTextContent('0.01 ETH')
    expect(legs).toHaveTextContent('Robinhood Chain')
    expect(screen.getByTestId('card-bridge-out')).toHaveTextContent('≈ 0.0099 ETH')
    expect(screen.getByTestId('card-bridge-fees')).toHaveTextContent('$0.12')
    // Both chains are facts; the solver address is not a recipient to read.
    expect(card).toHaveTextContent('From')
    expect(card).toHaveTextContent('To')
    expect(card).not.toHaveTextContent('0xa5f565650890fba1824ee0f21ebbbf660a179934')
    expect(screen.getByTestId('card-approve')).toBeInTheDocument()
  })

  it('a settled bridge links the deposit and the destination receipt', () => {
    renderDesk(
      <ApprovalCard
        order={bridgeOrder({
          status: 'confirmed',
          txHash: '0xdep',
          explorerUrl: 'https://basescan.org/tx/0xdep',
          receivedOut: '0.00985',
          bridge: {
            ...bridgeOrder().bridge!,
            requestId: 'req_1',
            fillStatus: 'success',
            depositTxHash: '0xdep',
            depositExplorerUrl: 'https://basescan.org/tx/0xdep',
            destinationTxHash: '0xfill',
            destinationExplorerUrl: 'https://explorer.robinhood.com/tx/0xfill',
          },
        })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('Bridge')
    expect(card).not.toHaveTextContent('Approval needed')
    expect(screen.queryByTestId('stamp-irreversible')).toBeNull()
    expect(screen.getByTestId('card-bridge-out')).toHaveTextContent('0.00985 ETH')
    expect(screen.getByTestId('card-bridge-out')).not.toHaveTextContent('≈')
    const txs = screen.getByTestId('card-bridge-txs')
    const links = txs.querySelectorAll('a[data-testid="card-tx"]')
    expect(links).toHaveLength(2)
    expect(links[0]).toHaveAttribute('href', 'https://basescan.org/tx/0xdep')
    expect(links[1]).toHaveAttribute('href', 'https://explorer.robinhood.com/tx/0xfill')
    expect(txs).toHaveTextContent('Deposit')
    expect(txs).toHaveTextContent('Arrival')
  })
})

describe('ApprovalCard for sends, batches and revokes', () => {
  const A = '0x2222222222222222222222222222222222222222'
  const B = '0x3333333333333333333333333333333333333333'
  const send = (extra: Parameters<typeof order>[0] = {}) =>
    order({
      kind: 'send',
      tokenOut: order().tokenIn,
      expectedOut: null,
      minOut: null,
      priceImpactPct: null,
      recipient: A,
      recipientLabel: null,
      amountIn: '0.1',
      valueUsd: 250,
      note: null,
      ...extra,
    })

  it('names a single send, prints the recipient in full and marks it irreversible', () => {
    renderDesk(
      <ApprovalCard
        order={send()}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveAttribute('data-kind', 'send')
    expect(card).toHaveTextContent('Approval needed · Send')
    expect(screen.getByTestId('stamp-irreversible')).toBeInTheDocument()
    expect(screen.getByTestId('card-legs')).toHaveTextContent('0.1 ETH')
    expect(screen.getByTestId('card-legs')).toHaveTextContent('0x2222…2222')
    expect(card).toHaveTextContent(A)
    expect(screen.queryByTestId('card-legs-list')).toBeNull()
    // The recipient fact is a wide row (its own line, never ellipsised).
    const wide = card.querySelector('.trd-card__fact--wide')
    expect(wide).not.toBeNull()
    expect(wide).toHaveTextContent(A)
    // The expiry fact: time and offset, never a year that pushes it off the row.
    expect(card).not.toHaveTextContent('2026')
  })

  it('shows a multisend as one card with every leg and one Approve for the batch', () => {
    const onApprove = vi.fn()
    const legs = [
      send({ orderId: 'a', batchId: 'bat_1', amountIn: '0.1', valueUsd: 250 }),
      send({ orderId: 'b', batchId: 'bat_1', amountIn: '0.15', valueUsd: 375, recipient: B }),
    ]
    renderDesk(
      <ApprovalsRegion
        pending={legs}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={onApprove}
        onReject={vi.fn()}
        focusOrderId={null}
      />,
    )
    const cards = screen.getAllByTestId('approval-card')
    expect(cards).toHaveLength(1)
    const card = cards[0]!
    expect(card).toHaveAttribute('data-batch', 'true')
    expect(card).toHaveTextContent('Approval needed · Multisend')
    expect(screen.getByTestId('card-legs')).toHaveTextContent('0.25 ETH')
    expect(screen.getByTestId('card-legs')).toHaveTextContent('2 recipients')
    const list = screen.getByTestId('card-legs-list')
    expect(list).toHaveTextContent(A)
    expect(list).toHaveTextContent(B)
    expect(card).toHaveTextContent('$625.00')
    // 625 USD is over the high-risk line: the batch total decides, not a leg.
    expect(screen.getByTestId('risk-high')).toBeInTheDocument()
    const approve = screen.getByTestId('card-approve')
    fireEvent.click(approve)
    fireEvent.click(approve)
    expect(onApprove).toHaveBeenCalledTimes(1)
    expect(onApprove.mock.calls[0]![0]).toMatchObject({ orderId: 'a' })
  })

  it('settles a batch as one stamp that shows the worst leg and each tx', () => {
    renderDesk(
      <ApprovalsRegion
        pending={[]}
        settled={[
          send({
            orderId: 'a',
            batchId: 'bat_2',
            status: 'confirmed',
            txHash: '0xaaaa1111',
            explorerUrl: 'https://basescan.org/tx/0xaaaa1111',
          }),
          send({
            orderId: 'b',
            batchId: 'bat_2',
            status: 'failed',
            reason: 'trading.tx_failed: nope',
            recipient: B,
          }),
        ]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
      />,
    )
    const stamps = screen.getAllByTestId('approval-stamp')
    expect(stamps).toHaveLength(1)
    expect(stamps[0]).toHaveTextContent('Multisend')
    expect(stamps[0]).toHaveTextContent('Failed')
    expect(screen.queryByTestId('card-approve')).toBeNull()
  })

  it('names a revoke by token and spender', () => {
    renderDesk(
      <ApprovalCard
        order={send({
          kind: 'revoke',
          amountIn: 'unlimited',
          valueUsd: 0,
          recipientLabel: 'Permit2',
        })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const card = screen.getByTestId('approval-card')
    expect(card).toHaveTextContent('Approval needed · Revoke')
    expect(screen.getByTestId('card-legs')).toHaveTextContent('ETH')
    expect(screen.getByTestId('card-legs')).toHaveTextContent('Permit2')
    expect(card).toHaveTextContent('unlimited')
    expect(screen.queryByTestId('stamp-irreversible')).toBeNull()
  })
})

describe('ApprovalCard · the note and the symbols are data, not facts', () => {
  it('labels the agent’s note and keeps a 5000-character one inside a bounded block', () => {
    const note = 'buy the dip '.repeat(420).slice(0, 5000)
    renderDesk(
      <ApprovalCard
        order={order({ note, initiator: 'agent' })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const block = screen.getByTestId('card-note')
    expect(block).toHaveTextContent('Agent’s note')
    const body = screen.getByTestId('card-note-body')
    expect(body).toHaveClass('trd-card__note-body')
    expect(body.textContent).toHaveLength(5000)
    expect(block.contains(body)).toBe(true)
    // The buttons are still there, after the note, and still work.
    const approve = screen.getByTestId('card-approve')
    const reject = screen.getByTestId('card-reject')
    expect(approve).toBeInTheDocument()
    expect(reject).toBeInTheDocument()
    expect(block.compareDocumentPosition(approve) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(approve).toBeEnabled()
  })

  it('calls a person’s note a note, not the agent’s', () => {
    renderDesk(
      <ApprovalCard
        order={order({ note: 'from the ticket', initiator: 'manual' })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const block = screen.getByTestId('card-note')
    expect(block).toHaveTextContent('Note')
    expect(block).not.toHaveTextContent('Agent’s note')
  })

  it('clamps a bidi-crafted symbol and isolates it, with the whole in the title', () => {
    // A "symbol" is whatever the token contract returns: a right-to-left
    // override could paint "0.2 ETH → 500 USDC" as something else.
    const symbol = 'USDC‮' + 'CDSU 000,01'.repeat(8)
    renderDesk(
      <ApprovalCard
        order={order({ tokenOut: { ...USDC, symbol } })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const legs = screen.getByTestId('card-legs')
    const syms = legs.querySelectorAll('.trd-sym')
    expect(syms).toHaveLength(2)
    const out = syms[1] as HTMLElement
    expect(Array.from(out.textContent ?? '')).toHaveLength(12)
    expect(out.textContent?.endsWith('…')).toBe(true)
    expect(out).toHaveAttribute('title', symbol)
    // The facts that carry the symbol are clamped the same way, whole in the title.
    const receive = Array.from(document.querySelectorAll('.trd-card__fact')).find((el) =>
      el.querySelector('dt')?.textContent?.startsWith('Receive'),
    )
    expect(receive).toBeDefined()
    const dd = receive?.querySelector('dd') as HTMLElement
    expect(dd).toHaveClass('trd-sym')
    expect(dd.textContent).toBe('500 USDC‮CDSU 0…')
    expect(dd).toHaveAttribute('title', `500 ${symbol}`)
    // An ordinary symbol carries no title on its fact row.
    const pay = Array.from(document.querySelectorAll('.trd-card__fact')).find((el) =>
      el.querySelector('dt')?.textContent?.startsWith('Pay'),
    )
    expect(pay?.querySelector('dd')).not.toHaveAttribute('title')
  })
})

describe('approvals region CSS contract', () => {
  // Innermost rules with comments dropped, keyed by selector.
  const deskCss = readFileSync('src/renderer/src/views/trading/desk/desk.css', 'utf8')
  const rules = [...deskCss.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(
    ([, selector = '', body = '']) => ({ selector: selector.trim(), body }),
  )
  const rule = (selector: string) => rules.find((r) => r.selector === selector)?.body ?? ''

  it('caps the region at a share of the window that fits an LP add card at 1200×800', () => {
    const region = rule('.trd-asks')
    const vh = Number(/max-height: min\((\d+)vh, [\d.]+rem\);/.exec(region)?.[1])
    // An LP add card is ~356px; 40vh (320px at 800px) clipped its buttons.
    expect(vh).toBeGreaterThanOrEqual(50)
    expect((vh / 100) * 800).toBeGreaterThan(356)
    expect(region).toMatch(/overflow-y: auto;/)
    // Its own layer above the composer block, and a gap before the chips.
    expect(region).toMatch(/position: relative;/)
    expect(Number(/z-index: (\d+);/.exec(region)?.[1])).toBeGreaterThan(0)
    expect(region).toMatch(/margin: 0 auto \d+px;/)
    expect(region).not.toMatch(/max-height: \d+(px|vh);/)
  })

  it('pins the decision row: the card body scrolls, Approve/Reject do not', () => {
    const actions = rule('.trd-card__actions')
    expect(actions).toMatch(/position: sticky;/)
    expect(actions).toMatch(/bottom: 0;/)
    // Opaque, so facts scrolling beneath do not read through the buttons.
    expect(actions).toMatch(/background: var\(--elevated\);/)
    // A settled stamp's tx row is not pinned.
    expect(rule('.trd-asks__stamp .trd-card__actions')).toMatch(/position: static;/)
  })
})

describe('ApprovalsRegion keeps the foot of the transcript in view', () => {
  // The region is a flex sibling of `.chat-thread`: whatever it gains comes
  // off the transcript's viewport at the bottom edge, so the transcript is
  // scrolled by the same amount. jsdom has no layout, so the region's height
  // and the ResizeObserver are driven by hand.
  let height = 0
  let observed: (() => void) | null = null
  let disconnects = 0

  beforeEach(() => {
    height = 0
    observed = null
    disconnects = 0
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          observed = cb
        }
        observe() {}
        disconnect() {
          disconnects += 1
        }
      },
    )
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: HTMLElement,
    ) {
      const h = this.classList.contains('trd-asks') ? height : 0
      return { height: h, width: 0, top: 0, left: 0, right: 0, bottom: h } as DOMRect
    })
    return () => {
      vi.unstubAllGlobals()
      vi.restoreAllMocks()
    }
  })

  const stage = (pending: ReturnType<typeof order>[]) => (
    <div className="chat-stage">
      <div className="chat-thread" data-testid="thread" />
      <ApprovalsRegion
        pending={pending}
        settled={[]}
        wallets={[WALLET]}
        deciding={null}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOrderId={null}
      />
    </div>
  )

  it('scrolls the transcript by what the region takes, and does nothing while it is empty', () => {
    const { rerender } = renderDesk(stage([]))
    const thread = screen.getByTestId('thread')
    let top = 500
    Object.defineProperty(thread, 'scrollTop', {
      configurable: true,
      get: () => top,
      set: (v: number) => {
        top = v
      },
    })
    // Empty: no region, no observer, no scroll.
    expect(observed).toBeNull()
    expect(top).toBe(500)

    // A card docks: the transcript pays back its full height before paint.
    height = 200
    rerender(stage([order({ orderId: 'p1' })]))
    expect(top).toBe(700)

    // A second card stacks: only the growth is paid.
    height = 320
    act(() => observed?.())
    expect(top).toBe(820)

    // Shrinking gives the transcript room below; the reader is not moved.
    height = 150
    act(() => observed?.())
    expect(top).toBe(820)
    // Growing again counts from where it shrank to, not from its peak.
    height = 180
    act(() => observed?.())
    expect(top).toBe(850)

    // Emptied: the observer goes, and a later ask counts from zero again.
    rerender(stage([]))
    expect(disconnects).toBe(1)
    expect(top).toBe(850)
    height = 100
    rerender(stage([order({ orderId: 'p2' })]))
    expect(top).toBe(950)
  })
})

/** The card wired to the real decision path, the way the desk wires it. */
function GatedCard({ o }: { o: Order }) {
  const decide = useOrderDecision()
  return (
    <ApprovalCard
      order={o}
      wallets={[WALLET]}
      deciding={decide.isPending}
      onApprove={(lead, legs) =>
        decide
          .mutateAsync({ orderId: lead.orderId, approve: true, order: lead, legs })
          .catch(() => {})
      }
      onReject={vi.fn()}
      focusOnMount={false}
    />
  )
}

describe('ApprovalCard · Touch ID', () => {
  it('says "Touch ID…" while the sheet is up for this card, and only this card', () => {
    renderDesk(
      <ApprovalCard
        order={order({ valueUsd: 50 })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const approve = screen.getByTestId('card-approve')
    act(() => useTouchIdPrompt.setState({ key: 'another-order' }))
    expect(approve).toHaveTextContent('Approve')
    act(() => useTouchIdPrompt.setState({ key: 'o1' }))
    expect(approve).toHaveTextContent('Touch ID…')
    expect(approve).toHaveAttribute('aria-busy', 'true')
    act(() => useTouchIdPrompt.setState({ key: null }))
    expect(approve).toHaveTextContent('Approve')
  })

  it('ignores a second click until the decision it started has settled', async () => {
    let settle: () => void = () => {}
    const onApprove = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve
        }),
    )
    renderDesk(
      <ApprovalCard
        order={order({ valueUsd: 50 })}
        wallets={[WALLET]}
        deciding={false}
        onApprove={onApprove}
        onReject={vi.fn()}
        focusOnMount={false}
      />,
    )
    const approve = screen.getByTestId('card-approve')
    fireEvent.click(approve)
    fireEvent.click(approve)
    fireEvent.click(approve)
    expect(onApprove).toHaveBeenCalledTimes(1)
    await act(async () => settle())
    fireEvent.click(approve)
    expect(onApprove).toHaveBeenCalledTimes(2)
  })

  it('High-risk: arms, then prompts; a cancel sends nothing and leaves the card live', async () => {
    setTouchId('high')
    let answer: (r: AuthResult) => void = () => {}
    authenticate.mockImplementation(
      () =>
        new Promise<AuthResult>((resolve) => {
          answer = resolve
        }),
    )
    renderDesk(<GatedCard o={order({ amountIn: '0.4', valueUsd: 900 })} />)
    const approve = screen.getByTestId('card-approve')
    fireEvent.click(approve)
    expect(authenticate).not.toHaveBeenCalled()
    expect(approve).toHaveTextContent('Click again to approve and execute')
    fireEvent.click(approve)
    await waitFor(() => expect(approve).toHaveTextContent('Touch ID…'))
    expect(authenticate).toHaveBeenCalledWith('approve 0.4 ETH → USDC on Base')
    expect(approve).toBeDisabled()
    // The sheet is up: another click is nothing.
    fireEvent.click(approve)
    expect(authenticate).toHaveBeenCalledTimes(1)

    await act(async () => answer({ ok: false, reason: 'cancelled' }))
    await waitFor(() => expect(approve).toHaveTextContent('Approve'))
    expect(approve).not.toBeDisabled()
    expect(screen.getByTestId('card-reject')).not.toBeDisabled()
    expect(rpcCall).not.toHaveBeenCalledWith('trading.orders.approve', expect.anything())

    // Try again and pass: the decision goes out exactly once.
    authenticate.mockResolvedValue({ ok: true })
    fireEvent.click(approve)
    fireEvent.click(approve)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('trading.orders.approve', { orderId: 'o1' }),
    )
    expect(rpcCall.mock.calls.filter(([m]) => m === 'trading.orders.approve')).toHaveLength(1)
    expect(authenticate).toHaveBeenCalledTimes(2)
  })

  it('High-risk: a low-risk approval goes out on one click with no prompt', async () => {
    setTouchId('high')
    renderDesk(<GatedCard o={order({ valueUsd: 50 })} />)
    fireEvent.click(screen.getByTestId('card-approve'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('trading.orders.approve', { orderId: 'o1' }),
    )
    expect(authenticate).not.toHaveBeenCalled()
  })

  it('Every approval: a low-risk one prompts too, and a failed prompt sends nothing', async () => {
    setTouchId('all')
    authenticate.mockResolvedValue({ ok: false, reason: 'failed' })
    renderDesk(<GatedCard o={order({ valueUsd: 50 })} />)
    const approve = screen.getByTestId('card-approve')
    fireEvent.click(approve)
    await waitFor(() => expect(authenticate).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(approve).not.toBeDisabled())
    expect(rpcCall).not.toHaveBeenCalledWith('trading.orders.approve', expect.anything())
  })
})
