import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { t } from '~/i18n'
import { History, isRevokeEntry, kindLabel } from './History'
import { renderDesk, USDC, WALLET } from './test-utils'
import type { Entry } from './types'

const rpcCall = vi.fn(async () => ({}) as unknown)
vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }),
}))
vi.mock('~/lib/desktop-api', () => ({
  desktopApi: () => ({ app: { openExternal: vi.fn(async () => {}) } }),
  isDesktop: () => true,
}))

function entry(extra: Partial<Entry> = {}): Entry {
  return {
    id: 'e1',
    ts: Date.now() - 60_000,
    chainId: 8453,
    wallet: WALLET.address,
    kind: 'approval',
    txHash: '0xabc',
    explorerUrl: 'https://basescan.org/tx/0xabc',
    tokenIn: USDC,
    amountIn: '0',
    tokenOut: null,
    amountOut: null,
    valueUsd: null,
    gasUsd: 0.01,
    initiator: 'manual',
    orderId: 'o1',
    note: 'revoked Permit2',
    ...extra,
  }
}

describe('History · a zero approval is a revoke', () => {
  it('reads a plain "Revoke" with the token, and no −0 amount', () => {
    renderDesk(<History entries={[entry()]} loading={false} now={Date.now()} showWallet={false} />)
    const row = screen.getByTestId('history-entry')
    expect(row).toHaveAttribute('data-revoke', 'true')
    expect(row.querySelector('.trd-entry__kind')).toHaveTextContent('Revoke')
    expect(row).not.toHaveTextContent('Approval')
    expect(row).not.toHaveTextContent('−0')
    expect(row).toHaveTextContent('USDC')
  })

  it('never names the spender from the note: the note is the agent’s free text', () => {
    // The ledger entry carries no spender field, and `--note` is whatever
    // the agent wrote. "revoked Permit2" on a revoke of some other spender
    // must not put "Permit2" on the row.
    renderDesk(
      <History
        entries={[entry({ note: 'revoked Permit2', initiator: 'agent' })]}
        loading={false}
        now={Date.now()}
        showWallet={true}
      />,
    )
    const row = screen.getByTestId('history-entry')
    expect(row.querySelector('.trd-entry__kind')).toHaveTextContent('Revoke')
    expect(row).not.toHaveTextContent('Permit2')
    expect(row).not.toHaveTextContent('revoked')
    // The wallet still leads the sub-line when asked for.
    expect(row.querySelector('.trd-entry__sub')).toHaveTextContent('0x1111…1111')
  })

  it('keeps the note on an entry that is not a revoke', () => {
    renderDesk(
      <History
        entries={[entry({ kind: 'swap', amountIn: '1', note: 'DCA leg 3' })]}
        loading={false}
        now={Date.now()}
        showWallet={false}
      />,
    )
    expect(screen.getByTestId('history-entry')).toHaveTextContent('DCA leg 3')
  })

  it('clamps and isolates a token symbol the chain handed it', () => {
    const symbol = 'USDC‮' + 'x'.repeat(40)
    renderDesk(
      <History
        entries={[entry({ kind: 'swap', amountIn: '1', tokenIn: { ...USDC, symbol } })]}
        loading={false}
        now={Date.now()}
        showWallet={false}
      />,
    )
    const sym = screen.getByTestId('history-entry').querySelector('.trd-sym')
    expect(sym).not.toBeNull()
    expect(sym?.textContent).toHaveLength(12)
    expect(sym?.textContent?.endsWith('…')).toBe(true)
    expect(sym).toHaveAttribute('title', symbol)
  })

  it('leaves a real allowance grant as an approval', () => {
    renderDesk(
      <History
        entries={[entry({ amountIn: '1000', note: null })]}
        loading={false}
        now={Date.now()}
        showWallet={false}
      />,
    )
    const row = screen.getByTestId('history-entry')
    expect(row.querySelector('.trd-entry__kind')).toHaveTextContent('Approval')
    expect(row).toHaveTextContent('−1,000 USDC')
  })

  it('exposes the rule', () => {
    expect(isRevokeEntry({ kind: 'approval', amountIn: '0' })).toBe(true)
    expect(isRevokeEntry({ kind: 'approval', amountIn: '0.0' })).toBe(true)
    expect(isRevokeEntry({ kind: 'approval', amountIn: null })).toBe(false)
    expect(isRevokeEntry({ kind: 'swap', amountIn: '0' })).toBe(false)
  })
})

describe('History · errors and paging', () => {
  it('says the read failed, with a retry, rather than "no activity"', () => {
    const onRetry = vi.fn()
    renderDesk(
      <History
        entries={[]}
        loading={false}
        now={Date.now()}
        showWallet={false}
        error={new Error('socket closed')}
        onRetry={onRetry}
      />,
    )
    expect(screen.queryByText('No activity yet')).toBeNull()
    expect(screen.getByText('Could not load this')).toBeInTheDocument()
    expect(screen.getByText('socket closed')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('trading-error-retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('loads the next page on the engine cursor and appends it', async () => {
    const older = entry({ id: 'e0', ts: Date.now() - 86_400_000 * 3, amountIn: '5', note: null })
    rpcCall.mockResolvedValue({ entries: [older], nextBefore: null })
    renderDesk(
      <History
        entries={[entry()]}
        loading={false}
        now={Date.now()}
        showWallet={false}
        nextBefore={1234}
        wallet={WALLET.address}
        chainId={8453}
      />,
    )
    expect(screen.getAllByTestId('history-entry')).toHaveLength(1)
    fireEvent.click(screen.getByTestId('history-more'))
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('trading.history', {
        wallet: WALLET.address,
        chainId: 8453,
        before: 1234,
        limit: 100,
      }),
    )
    await waitFor(() => expect(screen.getAllByTestId('history-entry')).toHaveLength(2))
    // The engine said that was the last page: the button is gone.
    expect(screen.queryByTestId('history-more')).toBeNull()
  })
})

describe('History · liquidity entries', () => {
  it('renders an lp_add row: its label, its glyph, and what left the wallet', () => {
    renderDesk(
      <History
        entries={[
          entry({
            kind: 'lp_add',
            amountIn: '25',
            note: 'Add liquidity · WETH/USDC · $50.00',
          }),
        ]}
        loading={false}
        now={Date.now()}
        showWallet={false}
      />,
    )
    const row = screen.getByTestId('history-entry')
    expect(row).toHaveAttribute('data-kind', 'lp_add')
    expect(row.querySelector('.trd-entry__kind')).toHaveTextContent('Add liquidity')
    expect(row.querySelector('.trd-entry__glyph svg.lucide-plus')).not.toBeNull()
    expect(row.querySelector('.trd-entry__out')).toHaveTextContent('−25')
    expect(row.querySelector('.trd-entry__in')).toBeNull()
  })

  it('renders collect and remove rows as what came back in', () => {
    renderDesk(
      <History
        entries={[
          // Explicit times: rows sort newest first, and two `Date.now()`
          // reads a millisecond apart once flipped them on CI.
          entry({
            id: 'c',
            ts: 1_700_000_120_000,
            kind: 'lp_collect',
            tokenIn: null,
            amountIn: null,
            tokenOut: USDC,
            amountOut: '3',
          }),
          entry({
            id: 'r',
            ts: 1_700_000_060_000,
            kind: 'lp_remove',
            tokenIn: null,
            amountIn: null,
            tokenOut: USDC,
            amountOut: '10',
          }),
        ]}
        loading={false}
        now={Date.now()}
        showWallet={false}
      />,
    )
    const [collect, remove] = screen.getAllByTestId('history-entry') as [HTMLElement, HTMLElement]
    expect(collect.querySelector('.trd-entry__kind')).toHaveTextContent('Collect fees')
    expect(collect.querySelector('.trd-entry__glyph svg.lucide-coins')).not.toBeNull()
    expect(collect.querySelector('.trd-entry__in')).toHaveTextContent('+3')
    expect(remove.querySelector('.trd-entry__kind')).toHaveTextContent('Remove liquidity')
    expect(remove.querySelector('.trd-entry__glyph svg.lucide-minus')).not.toBeNull()
    expect(remove.querySelector('.trd-entry__in')).toHaveTextContent('+10')
  })

  it('renders a kind this build does not know with a fallback glyph and the raw kind', () => {
    renderDesk(
      <History
        entries={[entry({ kind: 'lp_migrate' as Entry['kind'], amountIn: '1' })]}
        loading={false}
        now={Date.now()}
        showWallet={false}
      />,
    )
    const row = screen.getByTestId('history-entry')
    expect(row.querySelector('.trd-entry__kind')).toHaveTextContent('lp_migrate')
    expect(row.querySelector('.trd-entry__glyph svg')).not.toBeNull()
  })

  it('has a catalogue label for every entry kind', () => {
    const kinds: Entry['kind'][] = [
      'swap',
      'deposit',
      'withdraw',
      'approval',
      'gas',
      'unwrap',
      'lp_add',
      'lp_collect',
      'lp_remove',
    ]
    for (const kind of kinds) {
      expect(t(`trading.history.kind.${kind}`), kind).toBeTruthy()
    }
    expect(kindLabel('lp_add')).toBe('Add liquidity')
    expect(kindLabel('lp_collect')).toBe('Collect fees')
    expect(kindLabel('lp_remove')).toBe('Remove liquidity')
    expect(kindLabel('nope')).toBe('nope')
  })
})
