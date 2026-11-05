import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderDesk, USDC } from './test-utils'
import { POPULAR_TOKENS, TokenPicker } from './TokenPicker'
import type { Balance, Token } from './types'

vi.mock('@/app/providers', () => ({
  useRpc: () => ({
    call: vi.fn(async () => ({ tokens: [] })),
    waitForConnection: async () => {},
    on: () => () => {},
  }),
}))

const JUNK: Token = {
  chainId: 8453,
  address: '0x9999000000000000000000000000000000000077',
  symbol: 'CLAIM',
  name: 'Visit site to claim',
  decimals: 18,
  logoUrl: null,
  native: false,
  verified: false,
}

function balance(token: Token, amount: string, valueUsd: number | null): Balance {
  return { chainId: 8453, token, raw: amount, amount, priceUsd: null, valueUsd, change24hPct: null }
}

describe('TokenPicker · an empty query', () => {
  it('offers the gas coin with its logo even when the wallet holds none', () => {
    renderDesk(
      <TokenPicker
        chainId={4663}
        balances={[]}
        exclude={null}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    const eth = screen.getAllByTestId('token-pick')[0]
    expect(eth?.textContent).toContain('ETH')
    expect(eth?.querySelector('img')?.getAttribute('src')).toContain('ethereum.png')
  })

  it('lists verified holdings before airdrop junk, whatever the junk claims to be worth', () => {
    renderDesk(
      <TokenPicker
        chainId={8453}
        balances={[balance(JUNK, '1000000', 99_999), balance(USDC, '12', 12)]}
        exclude={null}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    const rows = screen.getAllByTestId('token-pick').map((r) => r.textContent ?? '')
    // USDC ($12) ahead of the unpriced gas coin, then the chain's popular
    // tokens (WETH; USDC is already held), and the $99,999 "airdrop" last.
    expect(rows[0]).toContain('USDC')
    expect(rows[1]).toContain('ETH')
    expect(rows[2]).toContain('WETH')
    expect(rows[rows.length - 1]).toContain('CLAIM')
    expect(rows[rows.length - 1]).not.toContain('WETH')
  })

  it('offers the chain’s popular tokens that are not already held', () => {
    const onPick = vi.fn()
    renderDesk(
      <TokenPicker
        chainId={8453}
        balances={[balance(USDC, '12', 12)]}
        exclude={null}
        onPick={onPick}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText('Popular')).toBeInTheDocument()
    const popular = screen
      .getAllByTestId('token-pick')
      .filter((r) => r.getAttribute('data-popular') === 'true')
      .map((r) => r.textContent ?? '')
    // USDC is held, so only WETH is left to suggest.
    expect(popular).toHaveLength(1)
    expect(popular[0]).toContain('WETH')
    fireEvent.click(
      screen.getAllByTestId('token-pick').find((r) => r.textContent?.includes('WETH'))!,
    )
    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({
        address: '0x4200000000000000000000000000000000000006',
        chainId: 8453,
      }),
    )
  })

  it('drops the popular group as soon as something is typed', () => {
    renderDesk(
      <TokenPicker
        chainId={8453}
        balances={[]}
        exclude={null}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText('Popular')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Search by name, symbol or address'), {
      target: { value: 'us' },
    })
    expect(screen.queryByText('Popular')).toBeNull()
  })

  it('carries the engine’s own Base addresses', () => {
    const base = POPULAR_TOKENS[8453]!.map((tk) => [tk.symbol, tk.address])
    expect(base).toEqual([
      ['USDC', '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'],
      ['WETH', '0x4200000000000000000000000000000000000006'],
    ])
  })
})
