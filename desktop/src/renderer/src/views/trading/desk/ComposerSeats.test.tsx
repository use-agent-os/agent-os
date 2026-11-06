import { readFileSync } from 'node:fs'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ComposerSeats } from './ComposerSeats'
import type { Limits, Wallet } from '../types'

const css = readFileSync('src/renderer/src/views/trading/desk/desk.css', 'utf8')

const limits: Limits = {
  thresholdUsd: 100,
  dailyCapUsd: 1000,
} as Limits

const wallet = {
  address: '0x89E0000000000000000000000000000000000a97',
  label: 'Main',
} as Wallet

function renderSeats() {
  return render(
    <ComposerSeats
      limits={limits}
      provider="aggregator"
      providers={[]}
      wallet={wallet}
      typing={false}
      onOpenSettings={vi.fn()}
      onOpenWallets={vi.fn()}
      onStartMission={vi.fn()}
      onQuick={vi.fn()}
      onSend={vi.fn()}
      onOpenTools={vi.fn()}
    />,
  )
}

// The seats row is a flex line whose natural width (~800px) is wider than the
// pane it lives in once the desk panel is open. `.trd-quick` does not shrink, so
// every missing pixel used to come out of the three seats — which carry
// `min-width: 0` and therefore collapsed all the way down. The row rendered as
// "A.", a bare circle, and an unlabelled route. The shrink chain in desk.css is
// what prevents that, and it only works if the markup gives it something to
// hide and something to keep.
describe('ComposerSeats shrink chain', () => {
  it('wraps every quick-action label in an element the chain can hide', () => {
    // The labels used to be bare text nodes, so `.trd-quick__chip > :not(svg)`
    // matched nothing and the whole step was dead code — the chips kept their
    // full width at every size and pushed the crush onto the seats.
    renderSeats()
    const chips = screen
      .getAllByRole('button')
      .filter((b) => b.className.includes('trd-quick__chip'))
    expect(chips.length).toBeGreaterThanOrEqual(4)
    for (const chip of chips) {
      expect(chip.querySelector('.trd-quick__chip-text')).not.toBeNull()
      // Hiding that span must not take the button's name with it.
      expect(chip.getAttribute('aria-label')).toBeTruthy()
    }
  })

  it('calls the wallet seat the active wallet', () => {
    renderSeats()
    const seat = screen.getByTestId('wallet-seat')
    expect(seat.getAttribute('title')).toBe('Active wallet')
    expect(seat.getAttribute('aria-label')).toMatch(/^Active wallet: /)
  })

  it('keeps an accessible name on each seat once its label can be hidden', () => {
    renderSeats()
    for (const id of ['permission-seat', 'provider-seat', 'wallet-seat']) {
      const seat = screen.getByTestId(id)
      expect(seat.getAttribute('aria-label')).toBeTruthy()
      expect(seat.querySelector('.trd-seat__text')).not.toBeNull()
    }
  })

  it('drops the labels one at a time, in descending width order', () => {
    // Each step must be strictly narrower than the one before it, or two labels
    // vanish at the same breakpoint and a whole size band loses information it
    // still had room for.
    const steps = [
      ['permission-seat', null],
      ['wallet-seat', null],
      ['provider-seat', null],
    ].map(([id]) => {
      const m = css.match(
        new RegExp(
          `@container \\(max-width: (\\d+)px\\) \\{\\s*(?:/\\*[\\s\\S]*?\\*/\\s*)?\\.trd-seat\\[data-testid='${id}'\\]`,
        ),
      )
      expect(m, `no shrink step for ${id}`).not.toBeNull()
      return Number(m?.[1])
    })
    const quick = Number(
      // `(?!@container)` keeps the match inside ONE block — a plain lazy gap
      // happily spans from the first breakpoint to the last selector.
      css.match(
        /@container \(max-width: (\d+)px\) \{(?:(?!@container)[\s\S])*?\.trd-quick__chip-text \{/,
      )?.[1],
    )
    expect(quick).toBeGreaterThan(0)
    const chain = [...steps, quick]
    expect(chain).toEqual([...chain].sort((a, b) => b - a))
    expect(new Set(chain).size).toBe(chain.length)
  })

  it('never lets a seat be addressed by position — the provider button is its own first child', () => {
    // `.trd-seat:first-child` matches the provider button too (it is the only
    // child of `.trd-seat__anchor`), which silently folded two chain steps into
    // one. Every step addresses its seat by test id instead.
    expect(css).not.toMatch(/\.trd-seat:first-child \.trd-seat__text/)
  })

  it('keeps glyphs at full size so a squeezed seat is still a readable icon', () => {
    expect(css).toMatch(
      /\.trd-seat > \*:not\(\.trd-seat__text\),[\s\S]*?\.trd-quick__chip > \*:not\(\.trd-quick__chip-text\) \{[\s\S]*?flex-shrink: 0;/,
    )
  })
})
