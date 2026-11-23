import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Holdings } from './Holdings'
import { ETH, holding, renderDesk, USDC } from './test-utils'
import type { Holding } from './types'

vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: vi.fn(), waitForConnection: async () => {}, on: () => () => {} }),
}))

const noop = () => {}

describe('Holdings · junk tokens', () => {
  it('says how many are hidden, and the toggle asks for them', () => {
    const onToggleHidden = vi.fn()
    renderDesk(
      <Holdings
        holdings={[holding()]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
        hiddenCount={3}
        onToggleHidden={onToggleHidden}
      />,
    )
    const chip = screen.getByTestId('junk-toggle')
    expect(chip.textContent).toBe('3junk')
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(chip)
    expect(onToggleHidden).toHaveBeenCalledTimes(1)
    expect(screen.getAllByTestId('holding-row')).toHaveLength(1)
  })

  it('renders a shown junk row as hidden, with a way to keep it', () => {
    const onSetHidden = vi.fn()
    const junk = holding({
      token: {
        chainId: 8453,
        address: '0x9999000000000000000000000000000000000077',
        symbol: 'CLAIM',
        name: 'Visit site to claim',
        decimals: 18,
        logoUrl: null,
        native: false,
        verified: false,
      },
      valueUsd: null,
      priceUsd: null,
      hidden: true,
    })
    renderDesk(
      <Holdings
        holdings={[holding(), junk]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
        hiddenCount={1}
        showHidden
        onToggleHidden={noop}
        onSetHidden={onSetHidden}
      />,
    )
    const rows = screen.getAllByTestId('holding-row')
    expect(rows).toHaveLength(2)
    expect(screen.getByTestId('junk-toggle').getAttribute('aria-pressed')).toBe('true')
    const junkRow = rows.find((r) => r.getAttribute('data-hidden') === 'true')
    expect(junkRow?.textContent).toContain('CLAIM')
    expect(junkRow?.textContent).toContain('junk')
    fireEvent.click(screen.getByTestId('show-token'))
    expect(onSetHidden).toHaveBeenLastCalledWith(junk, false)
    // A real holding offers the opposite.
    fireEvent.click(screen.getByTestId('hide-token'))
    expect(onSetHidden).toHaveBeenLastCalledWith(
      expect.objectContaining({ token: expect.objectContaining({ symbol: 'USDC' }) }),
      true,
    )
  })

  it('never lists a holding twice, whichever filters are open', () => {
    const dust = holding({
      token: {
        ...holding().token,
        address: '0x00000000000000000000000000000000000000d1',
        symbol: 'DUST',
      },
      valueUsd: 0.01,
    })
    const junk = holding({
      token: {
        ...holding().token,
        address: '0x00000000000000000000000000000000000000ee',
        symbol: 'JUNK',
      },
      valueUsd: null,
      hidden: true,
    })
    renderDesk(
      <Holdings
        holdings={[holding(), dust, junk]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
        hiddenCount={1}
        showHidden
        onToggleHidden={noop}
      />,
    )
    expect(screen.getAllByTestId('holding-row')).toHaveLength(2) // USDC + JUNK; DUST folded
    fireEvent.click(screen.getByTestId('dust-toggle'))
    const rows = screen.getAllByTestId('holding-row').map((r) => r.textContent ?? '')
    expect(rows).toHaveLength(3)
    expect(rows.filter((r) => r.includes('JUNK'))).toHaveLength(1)
  })

  it('says the read failed, with a retry, instead of "nothing held"', () => {
    const onRetry = vi.fn()
    renderDesk(
      <Holdings
        holdings={[]}
        loading={false}
        error={new Error('socket closed')}
        onRetry={onRetry}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    expect(screen.queryByText('Nothing held yet')).toBeNull()
    expect(screen.getByText('socket closed')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('trading-error-retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('names the wallet on each row once the table mixes wallets', () => {
    const other = '0x2222222222222222222222222222222222222222'
    const wallets = [holding().wallet!, other].map((address, i) => ({
      address,
      label: i === 0 ? 'Main' : 'Ops',
      primary: i === 0,
      createdAt: 0,
      chains: [8453],
    }))
    const { rerender } = renderDesk(
      <Holdings
        holdings={[holding()]}
        loading={false}
        wallets={wallets}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    // One wallet on screen: no label to clutter the cell.
    expect(screen.queryByTestId('asset-wallet')).toBeNull()
    rerender(
      <Holdings
        holdings={[holding(), holding({ wallet: other, amount: '5', valueUsd: 5 })]}
        loading={false}
        wallets={wallets}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    const chips = screen.getAllByTestId('asset-wallet').map((n) => n.textContent)
    expect(chips).toEqual(['Main', 'Ops'])
  })

  it('still shows the bar when nothing visible is held', () => {
    renderDesk(
      <Holdings
        holdings={[]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
        hiddenCount={2}
        onToggleHidden={noop}
      />,
    )
    expect(screen.getByTestId('hidden-bar').textContent).toContain('Hidden')
    expect(screen.getByTestId('junk-toggle').textContent).toBe('2junk')
  })
})

describe('Holdings · unverified tokens', () => {
  const unlisted = (symbol: string, last: string, extra: Partial<Holding> = {}): Holding =>
    holding({
      token: {
        ...USDC,
        address: `0x00000000000000000000000000000000000000${last}`,
        symbol,
        name: symbol,
        verified: false,
      },
      priceUsd: null,
      valueUsd: null,
      ...extra,
    })

  it('folds ⚠ tokens away by default, and the chip shows them', () => {
    renderDesk(
      <Holdings
        holdings={[holding(), unlisted('CHASE', 'c1'), unlisted('MACCA', 'c2')]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    const chip = screen.getByTestId('unverified-toggle')
    expect(chip.textContent).toBe('2unverified')
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    const before = screen.getAllByTestId('holding-row').map((r) => r.textContent ?? '')
    expect(before).toHaveLength(1)
    expect(before[0]).toContain('USDC')

    fireEvent.click(chip)
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    const after = screen.getAllByTestId('holding-row').map((r) => r.textContent ?? '')
    expect(after).toHaveLength(3)
    expect(after.filter((r) => r.includes('CHASE'))).toHaveLength(1)

    fireEvent.click(chip)
    expect(screen.getAllByTestId('holding-row')).toHaveLength(1)
  })

  it('folds a valuable ⚠ token too, and never the native coin', () => {
    renderDesk(
      <Holdings
        holdings={[
          holding({ token: { ...ETH, verified: false }, valueUsd: 50 }),
          unlisted('PUMP', 'c3', { priceUsd: 2, valueUsd: 500 }),
        ]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    const rows = screen.getAllByTestId('holding-row').map((r) => r.textContent ?? '')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toContain('ETH')
    expect(screen.getByTestId('unverified-toggle').textContent).toBe('1unverified')
  })

  it('puts each holding in one bucket: junk before unverified before dust', () => {
    const junk = unlisted('CLAIM', 'c4', { hidden: true })
    const tinyUnlisted = unlisted('TINY', 'c5', { priceUsd: 0.001, valueUsd: 0.01 })
    const dust = holding({
      token: { ...USDC, address: '0x00000000000000000000000000000000000000d1', symbol: 'DUST' },
      valueUsd: 0.01,
    })
    renderDesk(
      <Holdings
        holdings={[holding(), junk, tinyUnlisted, dust]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
        hiddenCount={1}
        showHidden
        onToggleHidden={noop}
      />,
    )
    expect(screen.getByTestId('unverified-toggle').textContent).toBe('1unverified')
    expect(screen.getByTestId('dust-toggle').textContent).toBe('1dust')
    // USDC + the junk row the junk chip already opened.
    expect(screen.getAllByTestId('holding-row')).toHaveLength(2)
    fireEvent.click(screen.getByTestId('unverified-toggle'))
    fireEvent.click(screen.getByTestId('dust-toggle'))
    const rows = screen.getAllByTestId('holding-row').map((r) => r.textContent ?? '')
    expect(rows).toHaveLength(4)
    for (const symbol of ['CLAIM', 'TINY', 'DUST']) {
      expect(rows.filter((r) => r.includes(symbol))).toHaveLength(1)
    }
  })
})

describe('Holdings · copy token address', () => {
  it('copies the contract address without selecting the row, and skips native ETH', async () => {
    const writeText = vi.fn(async () => {})
    Object.assign(navigator, { clipboard: { writeText } })
    const onSelect = vi.fn()
    renderDesk(
      <Holdings
        holdings={[holding(), holding({ token: ETH, amount: '0.5', valueUsd: 1250 })]}
        loading={false}
        selected={null}
        onSelect={onSelect}
        onSwap={noop}
        showChain
      />,
    )
    // One button: USDC has a contract, ETH is the chain's own coin.
    const copy = screen.getByTestId('asset-copy')
    expect(copy.getAttribute('aria-label')).toBe('Copy token address USDC')
    fireEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(USDC.address))
    expect(onSelect).not.toHaveBeenCalled()
  })
})

describe('Holdings · search', () => {
  const sym = (symbol: string, name: string, n: number, extra: Partial<Holding> = {}) =>
    holding({
      token: {
        ...USDC,
        symbol,
        name,
        address: `0x${String(n).padStart(40, '0')}`,
      },
      ...extra,
    })
  const six = [
    sym('AGENTOS', 'AgentOS', 1),
    sym('BNKR', 'BankrCoin', 2),
    sym('USDC', 'USD Coin', 3),
    sym('USDT', 'Tether USD', 4),
    sym('DEGEN', 'Degen', 5),
    holding({ token: ETH, amount: '0.5', valueUsd: 1250 }),
  ]

  it('stays out of a short table', () => {
    renderDesk(
      <Holdings
        holdings={six.slice(0, 5)}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    expect(screen.queryByTestId('holding-search')).toBeNull()
    expect(screen.queryByTestId('hidden-bar')).toBeNull()
  })

  it('narrows the table by symbol or name, and Escape brings it all back', () => {
    renderDesk(
      <Holdings
        holdings={six}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    const field = screen.getByTestId('holding-search')
    expect(screen.getAllByTestId('holding-row')).toHaveLength(6)
    fireEvent.change(field, { target: { value: 'usd' } })
    // USDC and USDT by symbol, Tether USD by name too: two rows, not three.
    const rows = screen.getAllByTestId('holding-row').map((r) => r.textContent ?? '')
    expect(rows).toHaveLength(2)
    expect(rows.some((r) => r.includes('USDC'))).toBe(true)
    expect(rows.some((r) => r.includes('USDT'))).toBe(true)
    fireEvent.change(field, { target: { value: 'bankr' } })
    expect(screen.getAllByTestId('holding-row')).toHaveLength(1)
    expect(screen.getByTestId('holding-row').textContent).toContain('BNKR')
    fireEvent.keyDown(field, { key: 'Escape' })
    expect((field as HTMLInputElement).value).toBe('')
    expect(screen.getAllByTestId('holding-row')).toHaveLength(6)
  })

  it('Return opens the first match', () => {
    const onSelect = vi.fn()
    renderDesk(
      <Holdings
        holdings={six}
        loading={false}
        selected={null}
        onSelect={onSelect}
        onSwap={noop}
        showChain
      />,
    )
    const field = screen.getByTestId('holding-search')
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onSelect).not.toHaveBeenCalled()
    fireEvent.change(field, { target: { value: 'degen' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect.mock.calls[0]?.[0].token.symbol).toBe('DEGEN')
  })

  it('never unfolds a bucket, and says when the folds hold a match', () => {
    const fake = sym('AGENTOS', 'AgentOS', 9, {
      token: {
        ...USDC,
        symbol: 'AGENTOS',
        name: 'AgentOS',
        address: `0x${'9'.repeat(40)}`,
        verified: false,
      },
    })
    renderDesk(
      <Holdings
        holdings={[...six, fake]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    const field = screen.getByTestId('holding-search')
    fireEvent.change(field, { target: { value: 'agentos' } })
    // The real one only: the ⚠ lookalike stays folded.
    expect(screen.getAllByTestId('holding-row')).toHaveLength(1)
    fireEvent.change(field, { target: { value: 'zzz' } })
    expect(screen.queryAllByTestId('holding-row')).toHaveLength(0)
    expect(screen.getByTestId('holding-search-none').textContent).toBe('No asset matches')
    fireEvent.change(field, { target: { value: 'agentos' } })
    fireEvent.click(screen.getByTestId('unverified-toggle'))
    expect(screen.getAllByTestId('holding-row')).toHaveLength(2)
  })

  it('points at the folds when only they hold a match', () => {
    const fake = holding({
      token: {
        ...USDC,
        symbol: 'CLAIM',
        name: 'Claim',
        address: `0x${'8'.repeat(40)}`,
        verified: false,
      },
    })
    renderDesk(
      <Holdings
        holdings={[...six, fake]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />,
    )
    fireEvent.change(screen.getByTestId('holding-search'), { target: { value: 'claim' } })
    expect(screen.queryAllByTestId('holding-row')).toHaveLength(0)
    expect(screen.getByTestId('holding-search-none').textContent).toBe(
      'No asset matches · 1 more folded away under the chips',
    )
  })
})
