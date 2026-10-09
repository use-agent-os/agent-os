import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/renderer/src/views/chat/chat.css', 'utf8')

// Two rules in this stylesheet only work because of a geometry fact that is
// easy to lose in a later edit: on the desktop skin `.msg.user` IS the bubble —
// it carries the padding and the background, and `.msg-body` sits INSIDE that
// padding. Anything positioned from the body's box therefore starts inside the
// bubble, and anything positioned from the column edge hangs outside the
// scroll container. Both bit us once; these guard the fix.
describe('desktop chat CSS geometry contract', () => {
  it('parks the user row hover actions in the outer gutter, not in the bubble padding', () => {
    // The shared rule places actions below the BODY (`top: calc(100% + 4px)`),
    // which is correct for assistant rows — their meta line is that 16px row.
    // On a user row the same offset lands on the bubble's own bottom padding,
    // sitting over the last line of the message and its rounded corner.
    expect(css).toMatch(/\.msg \.msg-body > \.msg-actions \{[\s\S]*?top: calc\(100% \+ 4px\);/)
    const userActions = css.match(/\.msg\.user \.msg-body > \.msg-actions \{[\s\S]*?\n\}/)?.[0]
    expect(userActions).toBeTruthy()
    expect(userActions).toMatch(/top: auto;/)
    expect(userActions).toMatch(/right: 100%;/)
    // Must clear the bubble's own 14px side padding plus a visible gap.
    const margin = Number(userActions?.match(/margin-right: (\d+)px;/)?.[1])
    const bubblePadding = Number(css.match(/\.msg\.user \{[\s\S]*?padding: \d+px (\d+)px;/)?.[1])
    expect(bubblePadding).toBeGreaterThan(0)
    expect(margin).toBeGreaterThan(bubblePadding)

    // …and a hover bridge wide enough to cross that gutter, or the pointer
    // leaves `.msg` on the way to the buttons and they vanish mid-reach.
    const bridge = css.match(/\.msg\.user \.msg-body > \.msg-actions::before \{[\s\S]*?\n\}/)?.[0]
    expect(bridge).toBeTruthy()
    expect(Number(bridge?.match(/width: (\d+)px;/)?.[1])).toBeGreaterThanOrEqual(margin)
  })

  it('reserves enough side padding for the gutter timestamp to survive overflow clipping', () => {
    // Rows sit flush with the column edge and `.msg::after` hangs the time
    // OUTSIDE them. The thread clips horizontally, so the side minimum has to
    // cover that overhang — at 24px the stamp was sliced ("13:59" → "13:")
    // whenever the desk panel narrowed the column enough for the minimum to win.
    expect(css).toMatch(/\.msg\.user::after \{[\s\S]*?right: -8px;/)
    expect(css).toMatch(/\.chat-thread \{[\s\S]*?overflow-x: hidden;/)
    const sideMin = Number(css.match(/\.chat-thread \{[\s\S]*?padding: \d+px max\((\d+)px,/)?.[1])
    expect(sideMin).toBeGreaterThanOrEqual(44)
  })

  it('keeps the jump-to-latest dock out of the transcript layout', () => {
    const dock = css.match(/\.chat-jump-dock \{[\s\S]*?\n\}/)?.[0]
    expect(dock).toMatch(/height: 0;/)
    expect(dock).toMatch(/pointer-events: none;/)
    // A flex item of `.chat-stage` now (above the approvals region): it may not grow.
    expect(dock).toMatch(/flex: none;/)
    expect(css).toMatch(/\.chat-jump-dock\[data-visible='false'\] \{[\s\S]*?visibility: hidden;/)
  })

  // At the desk the transcript can sit at its ~11rem floor under a docked
  // proposal; the floating pill then covered the header of the card the agent
  // had just posted. There the dock takes a line while it shows.
  it('gives the desk dock a line of its own while the pill shows, so it covers nothing', () => {
    const open = css.match(
      /^\.chat-desktop\[data-desk\] \.chat-jump-dock\[data-visible='true'\] \{[\s\S]*?^\}/m,
    )?.[0]
    expect(open).toMatch(/\bheight: (2[8-9]|[3-4]\d)px;/)
    const pill = css.match(
      /^\.chat-desktop\[data-desk\] \.chat-jump-dock \.chat-jump-to-latest \{[\s\S]*?^\}/m,
    )?.[0]
    expect(pill).toMatch(/position: static;/)
  })
})

// Every selector in the stylesheet, one per entry of a selector list (commas
// inside `:is()`/`:not()` stay put).
const selectors = (css.replace(/\/\*[\s\S]*?\*\//g, '').match(/[^{};]+(?=\{)/g) ?? []).flatMap(
  (prelude) => prelude.split(/,(?![^(]*\))/).map((s) => s.trim()),
)

// The turn footer (model, tokens, cache, cost) used to open on hover, from
// nothing to a 16px row plus a 4px margin, and take the glyph room on its
// right at the same moment: every row below jumped a line whenever the pointer
// crossed a message. The gutter timestamp was hover-only too.
describe('desktop chat response info', () => {
  it('keeps the footer and the timestamp on screen at rest, so hover never moves a row', () => {
    const meta = css.match(/^\.msg-meta \{[\s\S]*?^\}/m)?.[0]
    expect(meta).toBeTruthy()
    expect(meta).toMatch(/min-height: 16px;/)
    expect(meta).toMatch(/margin-top: 4px;/)
    expect(meta).not.toMatch(/opacity: 0;|\bheight: 0;|overflow: hidden;/)
    // The copy/retry glyphs park at the footer's trailing end, so their room
    // has to be there before they appear or the figures rewrap under them.
    const actions = css.match(/^\.msg \.msg-body > \.msg-actions \{[\s\S]*?^\}/m)?.[0]
    const glyph = Number(css.match(/^\.msg-action \{[\s\S]*?width: (\d+)px;/m)?.[1])
    const gap = Number(actions?.match(/gap: (\d+)px;/)?.[1])
    expect(glyph).toBeGreaterThan(0)
    expect(Number(meta?.match(/padding-right: (\d+)px;/)?.[1])).toBeGreaterThanOrEqual(
      2 * glyph + gap,
    )

    const stamp = css.match(/^\.msg::after \{[\s\S]*?^\}/m)?.[0]
    expect(stamp).toMatch(/content: attr\(data-time\);/)
    expect(stamp).not.toMatch(/opacity: 0;/)

    // Hovering or focusing a row reveals its actions, which are out of flow,
    // and nothing else.
    expect(actions).toMatch(/position: absolute;[\s\S]*?opacity: 0;/)
    const rowStates = selectors.filter((s) => /\.msg:(hover|focus-within)/.test(s))
    expect(rowStates).toContain('.msg:hover .msg-actions')
    expect(rowStates).toContain('.msg:focus-within .msg-actions')
    expect(rowStates.filter((s) => !s.endsWith(' .msg-actions'))).toEqual([])
    // However a hover state is spelled (`.msg.assistant:hover`, `:is()`), it
    // must not reach the footer or the stamp.
    expect(
      selectors.filter(
        (s) => /:(hover|focus-within)/.test(s) && /\.msg-meta\b|\.msg[^\s]*::after/.test(s),
      ),
    ).toEqual([])
  })

  it('keeps the footer off a streaming row until the turn completes', () => {
    expect(css).toMatch(/^\.msg\.streaming \.msg-meta \{\s*display: none;\s*\}/m)
  })
})

// The file chip is the console's markup (`file · name · mime`) drawn as a
// card: the shared renderer stamps `data-artifact-kind`, `-summary` and
// `-action` for exactly this, and the skin must keep reading them — the raw
// mime span is hidden, not reworded, and `.msg-body a` must not win the
// anchor back (it underlines it in lime).
describe('desktop artifact file card', () => {
  const card = css.match(/^\.msg-body \.msg-artifact-chip \{[\s\S]*?^\}/m)?.[0]

  it('outranks the transcript link rule and lays the card out as a grid', () => {
    expect(card).toBeTruthy()
    expect(card).toMatch(/display: grid;/)
    expect(card).toMatch(/text-decoration: none;/)
    expect(card).toMatch(/grid-template-areas:\s*'tile name action'\s*'tile summary action';/)
  })

  it('draws the subtitle and the button from the renderer-stamped attributes', () => {
    expect(css).toMatch(
      /^\.msg-body \.msg-artifact-chip::before \{[\s\S]*?content: attr\(data-artifact-summary\);/m,
    )
    expect(css).toMatch(
      /^\.msg-body \.msg-artifact-chip::after \{[\s\S]*?content: attr\(data-artifact-action\);/m,
    )
    expect(css).toMatch(/^\.msg-artifact-chip \.msg-file-chip__meta \{\s*display: none;\s*\}/m)
  })

  it('tints the tile per kind, with a glyph for every kind the renderer emits', () => {
    const tinted = selectors.filter((s) => /\.msg-artifact-chip\[data-artifact-kind=/.test(s))
    for (const kind of ['spreadsheet', 'document', 'pdf', 'presentation', 'archive', 'data']) {
      const rule = css.match(
        new RegExp(`\\.msg-artifact-chip\\[data-artifact-kind='${kind}'\\] \\{[\\s\\S]*?\\n\\}`),
      )?.[0]
      expect(rule, kind).toMatch(/--artifact-accent: var\(--(ok|info|danger|warn)\);/)
      expect(rule, kind).toMatch(/--artifact-glyph: url\('data:image\/svg\+xml,/)
    }
    expect(tinted.length).toBeGreaterThanOrEqual(6)
    // The glyph is a mask so it takes the accent, never a fixed colour.
    expect(css).toMatch(
      /^\.msg-artifact-chip \.msg-file-chip__icon::before \{[\s\S]*?mask: var\(--artifact-glyph\) center \/ contain no-repeat;/m,
    )
  })
})

// LP cards (frontend lp.ts) are the console's markup; the desktop draws them as
// desk instruments through the `data-lp-*` hooks the renderer stamps. These pin
// the hooks the skin depends on and the parts that make it the desk's, not the
// console's: a status rail rather than a border, figures in mono, and an
// explorer link that `.msg-body a` cannot repaint.
/**
 * Columns `repeat(auto-fit, minmax(clamp(A, (700px - 100%) * 999, B), 1fr))`
 * yields for four stats in a strip `width` px wide with a `gap` px column gap:
 * the most tracks n such that n·min + (n − 1)·gap ≤ width, capped at 4.
 */
function stripColumns(block: string, width: number, gap: number): number {
  const px = (v: string): number => (v.endsWith('rem') ? parseFloat(v) * 16 : parseFloat(v))
  const m =
    /minmax\(clamp\(calc\((\d+)% - ([\d.]+(?:px|rem))\), calc\(\((\d+)px - 100%\) \* (\d+)\), calc\((\d+)% - ([\d.]+(?:px|rem))\)\), 1fr\)/.exec(
      block.replace(/\s+/g, ' '),
    )
  if (!m) throw new Error('strip columns are not the quarter/half clamp')
  const lo = (Number(m[1]) / 100) * width - px(m[2]!)
  const flip = (Number(m[3]) - width) * Number(m[4])
  const hi = (Number(m[5]) / 100) * width - px(m[6]!)
  const min = Math.min(Math.max(flip, lo), hi)
  return Math.min(4, Math.floor((width + gap) / (min + gap)))
}

describe('desktop LP card skin', () => {
  const card = css.match(/^\.lp-card \{[\s\S]*?^\}/m)?.[0]

  it('draws the plate with a hairline and a status rail, not a border', () => {
    expect(card).toBeTruthy()
    expect(card).toMatch(/inset 3px 0 0 var\(--lp-rail\)/)
    expect(card).not.toMatch(/\bborder:/)
    expect(card).toMatch(/font-variant-numeric: tabular-nums;/)
  })

  it('tones the rail and the pill from the renderer-stamped status', () => {
    for (const status of ['in-range', 'above-range', 'below-range']) {
      expect(selectors, status).toContain(`.lp-card[data-lp-status='${status}']`)
      expect(selectors, status).toContain(`.lp-pill[data-lp-status='${status}']`)
    }
    expect(css).toMatch(/\.lp-card\[data-lp-status='in-range'\] \{\s*--lp-rail: var\(--ok\);/)
  })

  it('sets every figure in mono and keeps lime for the live bar', () => {
    expect(css).toMatch(
      /\.lp-stat__value,[\s\S]*?\.lp-row__value,[\s\S]*?font-family: var\(--font-mono\);/,
    )
    expect(css).toMatch(
      /\.lp-chart__bar\[data-active='true'\] \.lp-chart__fill \{\s*fill: var\(--primary\);/,
    )
  })

  it('outranks the transcript link rule for the explorer action', () => {
    const action = css.match(/^\.msg-body \.lp-card__action \{[\s\S]*?^\}/m)?.[0]
    expect(action).toMatch(/text-decoration: none;/)
    expect(action).toMatch(/color: var\(--muted-foreground\);/)
  })

  it('never changes the case of a token symbol', () => {
    // The pair and the price caption both carry symbols ("boar", "WETH per boar").
    for (const rule of ['.lp-card__pair', '.lp-chart__caption']) {
      const block = css.match(new RegExp(`^\\${rule} \\{[\\s\\S]*?^\\}`, 'm'))?.[0]
      expect(block, rule).toBeTruthy()
      expect(block, rule).not.toMatch(/text-transform/)
    }
  })

  it('keys the layout on the card, never on the host wrapper', () => {
    expect(selectors.some((s) => s.includes('.msg-artifact-lp[data-lp-kind'))).toBe(false)
    expect(selectors).toContain(".lp-card[data-lp-kind='positions']")
  })

  it('keeps position rows at desk density with fees and distance columns', () => {
    const rule = (selector: string): string | undefined =>
      css.match(
        new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{[\\s\\S]*?^\\}`, 'm'),
      )?.[0]
    const columns = (block: string | undefined): string[] | undefined =>
      block
        ?.match(/grid-template-columns:\s*([^;]+);/)?.[1]
        ?.trim()
        .split(/\s+(?![^(]*\))/)
    const row = rule('.lp-row')
    expect(row).toMatch(/min-height: 26px;/)
    // Every row shares the book's columns, so they line up and size to content.
    expect(row).toMatch(/grid-template-columns: subgrid;/)
    // Stacked (default, < 760px): two 26px-or-less lines —
    // status+distance | pair | value over chain | owner | fees.
    expect(row).toMatch(/grid-template-rows: 26px 22px;/)
    const book = rule('.lp-card .lp-rows')
    expect(columns(book)).toEqual(['max-content', 'minmax(0, 1fr)', 'max-content'])
    // Outranks `.msg-body :is(ul, ol)`, which indented the book by 1.3em.
    expect(book).toMatch(/padding: 0;/)
    expect(css).toMatch(/\.lp-card \.lp-row \+ \.lp-row \{\s*margin-top: 0;/)
    // Dense (the mounter measured >= 760px): one 26px line, six columns.
    expect(rule(".lp-card[data-lp-layout='dense'] .lp-row")).toMatch(/grid-template-rows: 26px;/)
    expect(columns(rule(".lp-card[data-lp-layout='dense'] .lp-rows"))).toHaveLength(6)
    expect(selectors).toContain('.lp-row__distance')
    expect(selectors).toContain('.lp-row__fees')
  })

  it('gives a narrow card (< 440px) three lines per row and one fact per line', () => {
    const rule = (selector: string): string | undefined =>
      css.match(
        new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{[\\s\\S]*?^\\}`, 'm'),
      )?.[0]
    const n = ".lp-card[data-lp-layout='narrow']"
    expect(rule(`${n} .lp-rows`)).toMatch(/grid-template-columns: minmax\(0, 1fr\);/)
    const row = rule(`${n} .lp-row`)
    expect(row).toMatch(/grid-template-columns: max-content minmax\(0, 1fr\) max-content;/)
    // Three lines; the pair's line grows if a long symbol wraps.
    expect(row).toMatch(/grid-template-rows: 24px minmax\(20px, auto\) 20px;/)
    // Rows no longer inherit the book's column gap, so they carry their own
    // (without it "ROBINHOOD CHAIN" ran into the owner).
    expect(row).toMatch(/column-gap: 10px;/)
    expect(rule(`${n} .lp-row__status`)).toMatch(/grid-area: 1 \/ 1 \/ 2 \/ 3;/)
    expect(rule(`${n} .lp-row__value`)).toMatch(/grid-area: 1 \/ 3;/)
    const pair = rule(`${n} .lp-row__pair`)
    expect(pair).toMatch(/grid-area: 2 \/ 1 \/ 3 \/ 3;/)
    expect(pair).toMatch(/overflow: visible;/)
    expect(pair).toMatch(/white-space: normal;/)
    expect(rule(`${n} .lp-row__fees`)).toMatch(/grid-area: 2 \/ 3;/)
    expect(rule(`${n} .lp-row__chain`)).toMatch(/grid-area: 3 \/ 1;/)
    expect(rule(`${n} .lp-row__wallet`)).toMatch(/grid-area: 3 \/ 2 \/ 4 \/ 4;/)
    // Facts one per line (label left, figure right), smaller figures.
    expect(css).toMatch(
      /\.lp-card\[data-lp-layout='narrow'\] \.lp-card__stats,\s*\.lp-card\[data-lp-layout='narrow'\] \.lp-card__stats\.lp-totals \{\s*grid-template-columns: minmax\(0, 1fr\);/,
    )
    expect(rule(`${n} .lp-stat__value`)).toMatch(/font-size: 11px;/)
    expect(css).not.toMatch(/data-lp-layout='narrow'\][^{]*\{[^}]*text-overflow: ellipsis/)
  })

  it('never cuts what a row means: only the owner may ellipsize', () => {
    const ellipsized = [...css.matchAll(/^(\.lp-row[^{]*) \{[^}]*text-overflow: ellipsis/gm)].map(
      (m) => m[1],
    )
    expect(ellipsized).toEqual(['.lp-row__wallet'])
    const status = css.match(/^\.lp-row__status \{[\s\S]*?^\}/m)?.[0]
    expect(status).not.toMatch(/overflow: hidden/)
    // The fixed 140px status column clipped "−100.0%" to "-100.".
    expect(css).not.toMatch(/grid-template-columns: 140px/)
  })

  it('styles the measured label states the mounter stamps', () => {
    expect(selectors).toContain(".lp-range[data-lp-bounds='stacked'] .lp-range__upper")
    expect(css).toMatch(/\.lp-range\[data-lp-now-wrap\] \.lp-range__now-side \{\s*display: block;/)
    expect(css).toMatch(/\.lp-chart__tick\[data-lp-hidden\] \{\s*visibility: hidden;/)
  })

  it('draws the link arrow only on an owner that is a link', () => {
    expect(selectors).not.toContain('.lp-row__wallet[data-lp-external]::before')
    expect(selectors).toContain('.lp-row__wallet[data-lp-link]::after')
    expect(selectors).toContain('.msg-body .lp-row__wallet[data-lp-link]')
  })

  it('lays the positions strip out 4-up from 700px and 2×2 below, never 3 + 1', () => {
    const strip = css.match(/^\.lp-card__stats\.lp-totals \{[\s\S]*?^\}/m)?.[0]
    expect(strip).toBeTruthy()
    const gap = 18 // .lp-card__stats gap: 0 18px
    // The live report: a 608px card (581px strip) wrapped FEES onto its own line.
    expect(stripColumns(strip!, 581, gap)).toBe(2)
    expect(stripColumns(strip!, 699, gap)).toBe(2)
    expect(stripColumns(strip!, 700, gap)).toBe(4)
    expect(stripColumns(strip!, 1000, gap)).toBe(4)
    for (let w = 240; w <= 1400; w += 1) expect(stripColumns(strip!, w, gap)).not.toBe(3)
  })

  it('greens unclaimed fees only when there are some', () => {
    expect(css).toMatch(/^\.lp-row__fees \{[^}]*color: var\(--muted-foreground\);/m)
    expect(css).toMatch(/^\.lp-row__fees\[data-lp-fees='positive'\] \{\s*color: var\(--ok\);/m)
    expect(css).toMatch(
      /^\.lp-hero\[data-lp-hero='fees'\] \.lp-hero__value\[data-lp-fees='positive'\] \{\s*color: var\(--ok\);/m,
    )
    expect(css).toMatch(
      /^\.lp-hero\[data-lp-hero='fees'\] \.lp-hero__value\[data-lp-fees='zero'\] \{\s*color: var\(--muted-foreground\);/m,
    )
    expect(css).not.toMatch(/lp-hero__value:not\(\[data-lp-no-price\]\)/)
  })

  it('shows both copy outcomes', () => {
    expect(css).toMatch(/\.lp-card__copy\[data-lp-copied='true'\] \{\s*color: var\(--ok\);/)
    expect(css).toMatch(/\.lp-card__copy\[data-lp-copied='failed'\] \{\s*color: var\(--danger\);/)
  })

  it('hangs the range bounds on the band edges, above the rule', () => {
    expect(css).toMatch(/\.lp-range__lower,\s*\.lp-range__upper \{[\s\S]*?position: absolute;/)
    expect(css).toMatch(/\.lp-range__upper \{\s*transform: translateX\(-100%\);/)
  })
})

// DCA cards (frontend dca.ts, docs/dca.md "Rendering") are the console's
// markup too; the desktop draws them as desk instruments through the
// `data-dca-*` hooks. These pin the hooks the skin reads and what makes it
// the desk's: a rail that follows the mandate's state (breathing while it
// waits for approval), figures in tabular mono, and actions that only exist
// at the desk and cannot be repainted by `.msg-body a`.
describe('desktop DCA card skin', () => {
  const rule = (selector: string): string | undefined =>
    css.match(
      new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{[\\s\\S]*?^\\}`, 'm'),
    )?.[0]

  it('joins the artifact grid next to the LP group', () => {
    expect(css).toMatch(
      /\.msg-artifact-lp-group,\s*\.msg-artifact-dca-group,\s*\.msg-artifact-trigger-group \{\s*display: grid;/,
    )
    expect(css).toMatch(/^\.msg-artifact-dca-group \{\s*max-width: min\(38rem, 100%\);/m)
  })

  it('draws a hairline plate with a status rail, not a border, and mono numerals', () => {
    const card = rule('.dca-card')
    expect(card).toBeTruthy()
    expect(card).toMatch(/--dca-rail: var\(--dim\);/)
    expect(card).toMatch(/box-shadow: inset 0 0 0 1px var\(--hairline\);/)
    expect(card).not.toMatch(/\bborder:/)
    expect(card).toMatch(/font-variant-numeric: tabular-nums;/)
    expect(rule('.dca-card::before')).toMatch(/background: var\(--dca-rail\);/)
    expect(css).toMatch(/\.dca-card__usd \{[^}]*font-family: var\(--font-mono\);/)
    expect(css).toMatch(
      /\.dca-stat__value,[\s\S]*?\.dca-run,[\s\S]*?font-family: var\(--font-mono\);/,
    )
  })

  it('tones the rail and the pill from every renderer-stamped status', () => {
    const tones: Record<string, string> = {
      awaiting_approval: 'warn',
      active: 'ok',
      completed: 'info',
    }
    for (const [status, tone] of Object.entries(tones)) {
      expect(css, status).toMatch(
        new RegExp(
          `\\.dca-card\\[data-dca-status='${status}'\\] \\{\\s*--dca-rail: var\\(--${tone}\\);`,
        ),
      )
      expect(css, status).toMatch(
        new RegExp(
          `\\.dca-pill\\[data-status='${status}'\\] \\{\\s*--dca-tone: var\\(--${tone}\\);`,
        ),
      )
    }
    for (const status of ['paused', 'stopped', 'rejected', 'expired']) {
      expect(selectors, status).toContain(`.dca-card[data-dca-status='${status}']`)
    }
  })

  it('breathes the rail while a mandate waits for approval, and holds still on reduced motion', () => {
    expect(rule(".dca-card[data-dca-status='awaiting_approval']::before")).toMatch(
      /animation: dca-rail-breathe/,
    )
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{\s*\.dca-card\[data-dca-status='awaiting_approval'\]::before,[\s\S]*?animation: none;/,
    )
  })

  it('keys layout, busy and due states on the card hooks, never on the host', () => {
    expect(selectors).toContain(".dca-card[data-dca-layout='wide'] .dca-card__stats")
    expect(selectors).toContain(".dca-card[data-dca-layout='narrow'] .dca-card__stats")
    expect(selectors).toContain(
      '.dca-card[data-dca-busy] > :not(.dca-actions):not(.dca-card__foot)',
    )
    expect(selectors).toContain('.dca-card__next[data-dca-next-at]')
    expect(selectors.some((s) => s.includes('.msg-artifact-dca[data-dca-kind'))).toBe(false)
    expect(selectors).toContain(".dca-card[data-dca-kind='mandates']")
  })

  it('styles every documented part of the card', () => {
    for (const part of [
      '.dca-card__head',
      '.dca-card__hero',
      '.dca-card__next',
      '.dca-card__progress',
      '.dca-card__stats',
      '.dca-chart',
      '.dca-runs',
      '.dca-actions',
      '.dca-actions__error',
      '.dca-card__warnings',
      '.dca-card__foot',
      '.dca-pill',
    ]) {
      expect(selectors, part).toContain(part)
    }
    // The reserved buy is hatched on the gauge, not a second solid colour.
    expect(rule('.dca-progress__reserved')).toMatch(/repeating-linear-gradient\(/)
  })

  it('sets the two plot lengths the renderer positions the chart labels from', () => {
    // dca.ts places the avg / now labels at calc(var(--dca-plot-pad) +
    // var(--dca-plot-h) * y): the skin must define both and build the plot
    // from them, or the labels float off their lines.
    const plot = rule('.dca-chart__plot')
    expect(plot).toMatch(/--dca-plot-pad: [\d.]+(px|rem);/)
    expect(plot).toMatch(/--dca-plot-h: [\d.]+(px|rem);/)
    expect(plot).toMatch(/padding-top: var\(--dca-plot-pad\);/)
    expect(plot).toMatch(/position: relative;/)
    expect(rule('.dca-chart__svg')).toMatch(/height: var\(--dca-plot-h\);/)
    expect(css).toMatch(
      /\.dca-chart__avg,\s*\.dca-chart__now,\s*\.dca-chart__fail \{\s*position: absolute;/,
    )
    expect(rule('.dca-chart__tooltip')).toMatch(/position: absolute;/)
    for (const kind of [
      'bar',
      'parked',
      'pending',
      'skip',
      'void',
      'fail',
      'avg-line',
      'now-line',
    ]) {
      expect(selectors, kind).toContain(`.dca-chart__${kind}`)
    }
  })

  it('spells out an armed Stop and marks the call in flight', () => {
    expect(rule('.msg-body .dca-action[data-dca-confirm]')).toMatch(/color: var\(--danger\);/)
    expect(selectors).toContain('.msg-body .dca-action[data-dca-pending]')
    expect(selectors).toContain('.dca-actions[data-dca-busy] .dca-action')
  })

  it('outranks the transcript link rule for the actions and makes Approve the one filled control', () => {
    expect(rule('.msg-body .dca-action')).toMatch(/cursor: default;/)
    expect(rule(".msg-body .dca-action[data-dca-tone='primary']")).toMatch(
      /background: var\(--primary\);/,
    )
    expect(rule(".msg-body .dca-action[data-dca-tone='danger']")).toMatch(/color: var\(--danger\);/)
    expect(rule('.msg-body .dca-card__action')).toMatch(/text-decoration: none;/)
  })

  it('shows a stale card: controls off, the as-of amber, the refresh still live', () => {
    const off = css.match(
      /^\.dca-card\[data-dca-stale\] \.dca-actions \.dca-action,[^{]*\{[\s\S]*?^\}/m,
    )?.[0]
    expect(off).toBeTruthy()
    expect(off).toMatch(/opacity: 0\.\d+;/)
    expect(off).toMatch(/pointer-events: none;/)
    // Only a failed re-read turns the as-of amber; the quiet mount-time
    // "checking" pass must not flash the footer.
    expect(selectors).toContain(".dca-card[data-dca-stale='failed'] .dca-card__ago")
    expect(selectors).not.toContain('.dca-card[data-dca-stale] .dca-card__ago')
    expect(css).toMatch(
      /\.dca-card\[data-dca-stale='failed'\] \.dca-card__as-of,\s*\.dca-card\[data-dca-stale='failed'\] \.dca-card__ago \{\s*color: var\(--warn\);/,
    )
    expect(rule('.dca-card__stale')).toMatch(/display: flex;/)
    // The footer's ↻ is how a stale card recovers: nothing may switch it off.
    expect(
      selectors.some((sel) => /data-dca-stale\][^,]*\.dca-card__(action|refresh)/.test(sel)),
    ).toBe(false)
  })

  it('leads a list row with the mandate name in the text face, figures still mono', () => {
    const name = rule('.dca-row__name')
    expect(name).toMatch(/font-family: var\(--font-sans\);/)
    expect(name).toMatch(/font-weight: 500;/)
    expect(name).toMatch(/order: -1;/)
    expect(name).toMatch(/text-overflow: ellipsis;/)
    expect(name).not.toMatch(/text-transform/)
    expect(css).toMatch(
      /\.dca-row__spent,\s*\.dca-card__foot \{\s*font-family: var\(--font-mono\);/,
    )
  })

  it("hangs quiet mono y-axis labels at the plot's left edge", () => {
    const axis = rule('.dca-chart__y')
    expect(axis).toMatch(/position: absolute;/)
    expect(axis).toMatch(/pointer-events: none;/)
    const label = rule('.dca-chart__ylabel')
    expect(label).toMatch(/position: absolute;/)
    expect(label).toMatch(/left: 0;/)
    expect(label).toMatch(/font-family: var\(--font-mono\);/)
    expect(label).toMatch(/color: var\(--dim\);/)
    expect(Number(label?.match(/font-size: ([\d.]+)px;/)?.[1])).toBeLessThanOrEqual(10)
  })

  it('keeps the tooltip legible wherever the renderer places it', () => {
    const tip = rule('.dca-chart__tooltip')
    expect(tip).toMatch(/background: var\(--elevated\);/)
    expect(tip).toMatch(/border: 1px solid var\(--border\);/)
    expect(tip).toMatch(/backdrop-filter: blur\(\d+px\);/)
    // Above the bars and every plot label (the y axis sits at 1).
    const z = Number(tip?.match(/z-index: (\d+);/)?.[1])
    expect(z).toBeGreaterThan(Number(rule('.dca-chart__y')?.match(/z-index: (\d+);/)?.[1] ?? 0))
    // Placed under the columns inline: the skin's default `bottom` must yield.
    expect(rule(".dca-chart__tooltip[data-dca-place='below']")).toMatch(/bottom: auto;/)
  })

  it('never changes the case of a token symbol', () => {
    for (const r of ['.dca-card__pair,', '.dca-card__name', '.dca-stat__symbol']) {
      const block = css.match(new RegExp(`^\\${r}[^{]*\\{[\\s\\S]*?^\\}`, 'm'))?.[0]
      expect(block, r).toBeTruthy()
      expect(block, r).not.toMatch(/text-transform/)
    }
  })
})

// Trigger cards (frontend trigger.ts, docs/triggers.md "Rendering") are the
// console's markup; the desktop draws them as desk instruments through the
// `data-trigger-*` hooks. These pin the class inventory the contract names
// and what makes the skin the desk's: a rail that follows the trigger's
// state, the kind glyph drawn by CSS (never in the DOM), a price rail tinted
// amber near its line, and actions keyed on `data-trigger-op`.
describe('desktop trigger card skin', () => {
  const rule = (selector: string): string | undefined =>
    css.match(
      new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{[\\s\\S]*?^\\}`, 'm'),
    )?.[0]

  it('joins the artifact grid next to the DCA group', () => {
    expect(css).toMatch(/^\.msg-artifact-trigger-group \{\s*max-width: min\(38rem, 100%\);/m)
  })

  it('draws a hairline plate with a status rail, not a border, and mono numerals', () => {
    const card = rule('.trigger-card')
    expect(card).toBeTruthy()
    expect(card).toMatch(/--trigger-rail: var\(--dim\);/)
    expect(card).toMatch(/box-shadow: inset 0 0 0 1px var\(--hairline\);/)
    expect(card).not.toMatch(/\bborder:/)
    expect(card).toMatch(/font-variant-numeric: tabular-nums;/)
    expect(rule('.trigger-card::before')).toMatch(/background: var\(--trigger-rail\);/)
    expect(css).toMatch(
      /\.trigger-card__facts dd,\s*\.trigger-fact__value,\s*\.trigger-fires,\s*\.trigger-card__foot \{\s*font-family: var\(--font-mono\);/,
    )
  })

  it('tones the rail and the pill from every status the contract names', () => {
    const tones: Record<string, string> = {
      awaiting_approval: 'warn',
      armed: 'ok',
      paused: 'muted-foreground',
    }
    for (const [status, tone] of Object.entries(tones)) {
      expect(css, status).toMatch(
        new RegExp(
          `\\.trigger-card\\[data-trigger-status='${status}'\\] \\{\\s*--trigger-rail: var\\(--${tone}\\);`,
        ),
      )
    }
    for (const status of ['triggered', 'done']) {
      expect(selectors, status).toContain(`.trigger-card[data-trigger-status='${status}']`)
      expect(selectors, status).toContain(`.trigger-pill[data-status='${status}']`)
    }
    for (const status of ['stopped', 'rejected', 'expired']) {
      expect(selectors, status).toContain(`.trigger-card[data-trigger-status='${status}']`)
      expect(selectors, status).toContain(`.trigger-pill[data-status='${status}']`)
    }
    expect(css).toMatch(/\.trigger-pill\[data-status='armed'\] \{\s*--trigger-tone: var\(--ok\);/)
  })

  it('pulses awaiting and triggered, and holds still on reduced motion', () => {
    expect(selectors).toContain(".trigger-card[data-trigger-status='awaiting_approval']::before")
    expect(selectors).toContain(".trigger-card[data-trigger-status='triggered']::before")
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{\s*\.trigger-card\[data-trigger-status='awaiting_approval'\]::before,[\s\S]*?animation: none;/,
    )
  })

  it('draws the kind glyph from data-trigger-action, never an emoji', () => {
    expect(rule(".trigger-card[data-trigger-action='sell'] .trigger-card__head::before")).toMatch(
      /content: '▼';/,
    )
    expect(rule(".trigger-card[data-trigger-action='buy'] .trigger-card__head::before")).toMatch(
      /content: '▲';/,
    )
    const alert = rule(".trigger-card[data-trigger-action='alert'] .trigger-card__head::before")
    expect(alert).toMatch(/content: '.';/u)
    expect(alert).not.toMatch(/\p{Extended_Pictographic}/u)
  })

  it('tints the price rail amber within 1 % of its line', () => {
    const near = css.match(
      /(\.trigger-gauge\[data-trigger-[^{]+)\{\s*--trigger-gauge-tone: var\(--warn\);/,
    )
    expect(near).toBeTruthy()
    const list = near?.[1] ?? ''
    // The renderer classifies the signed distance itself (trigger.ts
    // `triggerGauge`): `near` within 1 % of the line, `met` once it is crossed.
    for (const hook of ["[data-trigger-proximity='near']", "[data-trigger-proximity='met']"]) {
      expect(list, hook).toContain(hook)
    }
  })

  it('keys layout, busy and stale states on the card hooks', () => {
    expect(selectors).toContain(".trigger-card[data-trigger-layout='wide'] .trigger-card__facts")
    expect(selectors).toContain(".trigger-card[data-trigger-layout='narrow'] .trigger-card__facts")
    expect(selectors).toContain(
      '.trigger-card[data-trigger-busy] > :not(.trigger-actions):not(.trigger-card__foot)',
    )
    expect(css).toMatch(
      /\.trigger-card\[data-trigger-stale\] \.trigger-actions \[data-trigger-op\],[^{]*\{[^}]*pointer-events: none;/,
    )
    // The footer's ↻ is how a stale card recovers: nothing may switch it off.
    expect(
      selectors.some((sel) => /data-trigger-stale\][^,]*\.trigger-card__foot (a|button)/.test(sel)),
    ).toBe(false)
    expect(selectors).toContain(".trigger-card[data-trigger-kind='triggers']")
  })

  it('styles every documented part of the card', () => {
    for (const part of [
      '.trigger-card__head',
      '.trigger-card__hero',
      '.trigger-card__now',
      '.trigger-gauge',
      '.trigger-card__facts',
      '.trigger-fires',
      '.trigger-actions',
      '.trigger-actions__error',
      '.trigger-card__warnings',
      '.trigger-card__foot',
      '.trigger-pill',
    ]) {
      expect(selectors, part).toContain(part)
    }
  })

  it('makes Approve & arm the one filled control and spells out an armed Stop or Fire now', () => {
    expect(rule(".msg-body .trigger-actions [data-trigger-op='approve']")).toMatch(
      /background: var\(--primary\);/,
    )
    expect(rule('.msg-body .trigger-actions [data-trigger-op]')).toMatch(/cursor: default;/)
    expect(rule('.msg-body .trigger-actions [data-trigger-op][data-trigger-confirm]')).toMatch(
      /color: var\(--danger\);/,
    )
    expect(selectors).toContain(
      ".msg-body .trigger-actions [data-trigger-op='fire'][data-trigger-confirm]",
    )
    expect(selectors).toContain(
      '.msg-body .trigger-actions [data-trigger-op][data-trigger-pending]',
    )
  })

  // A done fill's detail lost its "@ $1.00" to an ellipsis: the one figure
  // that says what the trigger filled at.
  it('wraps a fire’s detail rather than ellipsizing it', () => {
    const detail = rule('.trigger-fire__detail')
    expect(detail).toMatch(/white-space: normal;/)
    expect(detail).toMatch(/min-width: 0;/)
    expect(detail).not.toMatch(/text-overflow: ellipsis;/)
    expect(detail).not.toMatch(/overflow: hidden;/)
  })

  // A bracket's "0.5 % at take-profit, 1 % at stop" ran over the next fact
  // in the four-column row: the figure wraps in its own cell, and a long one
  // (the renderer marks it) takes two columns — one per line when narrow.
  it('wraps a fact’s figure inside its cell, and lets a long fact span two columns', () => {
    const value = css.match(
      /^\.trigger-card__facts dd,\s*\.trigger-fact__value \{[\s\S]*?^\}/m,
    )?.[0]
    expect(value).toMatch(/min-width: 0;/)
    expect(value).toMatch(/max-width: 100%;/)
    expect(value).toMatch(/white-space: normal;/)
    expect(value).toMatch(/overflow-wrap: anywhere;/)
    expect(value).not.toMatch(/white-space: nowrap;|text-overflow: ellipsis;/)
    expect(rule('.trigger-fact')).toMatch(/min-width: 0;/)
    expect(rule(".trigger-fact[data-trigger-fact-span='2']")).toMatch(/grid-column: span 2;/)
    expect(
      rule(".trigger-card[data-trigger-layout='narrow'] .trigger-fact[data-trigger-fact-span='2']"),
    ).toMatch(/grid-column: 1 \/ -1;/)
    // The estimate is its own quieter line; the text-only " · " never shows.
    expect(rule('.trigger-fact__sub')).toMatch(/display: block;/)
    expect(rule('.trigger-fact__value > .trigger-sep')).toMatch(/display: none;/)
  })

  it('keeps the proposal countdown from orphaning its last unit', () => {
    expect(rule('.trigger-card__now')).toMatch(/text-wrap: pretty;/)
  })

  it('never changes the case of a token symbol in the sentence or the facts', () => {
    for (const r of ['.trigger-card__hero', '.trigger-card__facts dd,']) {
      const block = css.match(new RegExp(`^\\${r}[^{]*\\{[\\s\\S]*?^\\}`, 'm'))?.[0]
      expect(block, r).toBeTruthy()
      expect(block, r).not.toMatch(/text-transform/)
    }
  })
})

// The shared renderer is the class inventory: every class it emits must be
// drawn by the desk skin, or that part renders bare — the live desk once ran
// "trên 0.5Base" together in the header, put a list row's status dot on a line
// of its own and printed "↻refresh⧉copy id" as one word, all from rules that
// were never written. Read the renderer itself so a class added there fails
// here until the skin draws it.
describe('desktop trigger skin covers the shared renderer', () => {
  // Beside this file: views/chat → the repo root is six levels up. (jsdom
  // gives `import.meta.url` an http scheme, so the path comes from __dirname.)
  const renderer = readFileSync(
    resolve(__dirname, '../../../../../../frontend/src/views/chat/transcript/trigger.ts'),
    'utf8',
  )
  // Every quoted or template literal, split into class-shaped tokens: the
  // `el(tag, 'a b')` class names, the SVG `class:` values and the selectors the
  // mounter queries. `data-trigger-*` attribute names start with `data-` and
  // a path like `trigger-cards/` ends in a slash, so neither counts.
  const literals = renderer.match(/'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/g) ?? []
  const emitted = new Set(
    literals.flatMap((lit) =>
      [
        ...lit.matchAll(
          /(?<![\w-])(?:msg-artifact-trigger(?:(?:-{1,2}|__)[a-z0-9]+)*|trigger(?:(?:-{1,2}|__)[a-z0-9]+)+)(?![\w/-])/g,
        ),
      ].map((m) => m[0]),
    ),
  )
  const drawn = (cls: string): boolean => {
    const hook = new RegExp(`\\.${cls}(?![\\w-])`)
    return selectors.some((sel) => hook.test(sel))
  }

  it('reads the inventory off the renderer', () => {
    // A broken extraction must not pass by finding nothing to check.
    expect(emitted.size).toBeGreaterThan(60)
    for (const cls of [
      'trigger-card__title',
      'trigger-chain',
      'trigger-pill__dot',
      'trigger-row__dot',
      'trigger-row__main',
      'trigger-card__actions',
      'trigger-card__action-glyph',
      'trigger-sep',
      'msg-artifact-trigger__body',
    ]) {
      expect(emitted, cls).toContain(cls)
    }
  })

  it('has at least one rule for every class the renderer emits', () => {
    const missing = [...emitted].filter((cls) => !drawn(cls))
    expect(missing).toEqual([])
  })

  it('lays the list row out as a grid with the dot in its own column', () => {
    const row = css.match(/^\.trigger-row \{[\s\S]*?^\}/m)?.[0]
    expect(row).toMatch(/display: grid;/)
    expect(row).toMatch(/grid-template-columns: max-content minmax\(0, 1fr\)/)
    expect(css).toMatch(/^\.trigger-row__dot \{[^}]*width: 6px;/m)
    // The old ::before dot was a flex item that wrapped onto its own line.
    expect(selectors).not.toContain('.trigger-row::before')
  })

  it('spaces the header, the pill dot and the footer actions', () => {
    expect(css).toMatch(/^\.trigger-card__title \{[^}]*display: flex;[^}]*gap: \d+px;/m)
    expect(css).toMatch(/^\.trigger-pill__dot \{[^}]*background: currentColor;/m)
    expect(css).toMatch(/^\.trigger-card__actions \{[^}]*gap: \d+px;/m)
    expect(css).toMatch(/^\.msg-body \.trigger-card__action \{[^}]*gap: \d+px;/m)
    // A list head holds only its title: it must not be pushed to the right.
    expect(selectors).not.toContain('.trigger-card__head > :last-child')
  })

  it('colours a fire by the attribute the renderer stamps', () => {
    expect(renderer).toMatch(/row\.dataset\.triggerFireStatus = /)
    expect(selectors).toContain(
      ".trigger-fire[data-trigger-fire-status='failed'] .trigger-fire__status",
    )
    expect(selectors.some((sel) => sel.includes('.trigger-fire[data-status='))).toBe(false)
  })
})

// Brackets (docs/brackets.md, "Rendering"): the shared renderer draws a
// take-profit + stop-loss pair as a trigger card keyed `bracket`, and the
// contract names every hook the desk must skin blind. These pin each of them,
// and what makes the range gauge the desk's: a faint zone, a red-tinted stop
// tick, a green-tinted take-profit tick, an amber dot near either line.
describe('desktop bracket card skin', () => {
  const rule = (selector: string): string | undefined =>
    css.match(
      new RegExp(`^${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{[\\s\\S]*?^\\}`, 'm'),
    )?.[0]

  it('styles every hook the contract names', () => {
    for (const sel of [
      ".trigger-card[data-trigger-kind='bracket'] .trigger-card__head::before",
      ".trigger-gauge[data-trigger-gauge='range']",
      ".trigger-gauge[data-trigger-gauge='range'] .trigger-gauge__line",
      ".trigger-gauge .trigger-gauge__tick[data-leg='sl']",
      ".trigger-gauge .trigger-gauge__tick[data-leg='tp']",
      ".trigger-gauge__zone[data-zone='bracket']",
      '.trigger-gauge__dot',
      ".trigger-gauge__label[data-leg='sl']",
      ".trigger-gauge__label[data-leg='tp']",
      ".trigger-gauge__label[data-leg='now']",
      ".trigger-card[data-trigger-nearest='tp'] .trigger-gauge__tick[data-leg='tp']",
      ".trigger-card[data-trigger-nearest='sl'] .trigger-gauge__tick[data-leg='sl']",
      '.bracket-legs',
      '.bracket-leg',
      ".bracket-leg[data-leg='tp'] .bracket-leg__word",
      ".bracket-leg[data-leg='sl'] .bracket-leg__word",
      '.bracket-leg__word',
      '.bracket-leg__line',
      '.bracket-leg__state',
      ".bracket-leg[data-status='armed']",
      '.trigger-card__group',
      '.trigger-fact__rr',
      ".trigger-card[data-trigger-kind='brackets']",
      '.trigger-row[data-bracket-id] .trigger-row__plan',
    ]) {
      expect(selectors, sel).toContain(sel)
    }
  })

  it('tints the stop tick red, the take-profit tick green, and the zone faint', () => {
    expect(rule(".trigger-gauge .trigger-gauge__tick[data-leg='sl']")).toMatch(/--danger/)
    expect(rule(".trigger-gauge .trigger-gauge__tick[data-leg='tp']")).toMatch(/--ok/)
    expect(css).toMatch(
      /\.trigger-gauge__zone\[data-zone='bracket'\] \{\s*fill: color-mix\(in srgb, var\(--foreground\) \d+%, transparent\);/,
    )
    // The rail is the plain rail, not the trigger's coloured line.
    expect(rule(".trigger-gauge[data-trigger-gauge='range'] .trigger-gauge__line")).toMatch(
      /stroke: var\(--border\);/,
    )
  })

  it('turns the dot amber within 1 % of either line, from data-trigger-dist', () => {
    const near = css.match(
      /(\.trigger-gauge\[data-trigger-gauge='range'\][^{]+)\{\s*--trigger-gauge-tone: var\(--warn\);/,
    )
    expect(near).toBeTruthy()
    const list = near?.[1] ?? ''
    for (const hook of [
      "[data-trigger-dist^='0']",
      "[data-trigger-dist^='-0']",
      "[data-trigger-dist='1']",
      "[data-trigger-dist='-1']",
      "[data-trigger-proximity='near']",
    ]) {
      expect(list, hook).toContain(hook)
    }
    // Never "1.5" or "10": a bare `^='1'` prefix would tint far lines.
    expect(list).not.toContain("[data-trigger-dist^='1']")
  })

  it('draws the ▼▲ pair for a sell bracket, never an emoji', () => {
    const sell = rule(
      ".trigger-card[data-trigger-kind='bracket'][data-trigger-action='sell'] .trigger-card__head::before",
    )
    expect(sell).toMatch(/content: '▼▲';/)
    expect(sell).toMatch(/background-clip: text;/)
    // Prettier wraps this selector after the attribute pair.
    const alert = css.match(
      /^\.trigger-card\[data-trigger-kind='bracket'\]\[data-trigger-action='alert'\]\s+\.trigger-card__head::before \{[^}]*\}/m,
    )?.[0]
    expect(alert).toMatch(/content: '.';/u)
    expect(alert).not.toMatch(/\p{Extended_Pictographic}/u)
  })

  it('lays the legs strip out as two mono rows, ruled, the state ellipsizing', () => {
    expect(rule('.bracket-legs')).toMatch(/font-family: var\(--font-mono\);/)
    const leg = rule('.bracket-leg')
    expect(leg).toMatch(/display: grid;/)
    expect(leg).toMatch(/border-bottom: 1px solid var\(--hairline\);/)
    expect(rule('.bracket-leg__state')).toMatch(/text-overflow: ellipsis;/)
  })
})

// The ask_user and exit_plan_mode cards come from the shared renderer
// (transcript/ask.ts, transcript/plan.ts), which builds their glyphs as bare
// <svg viewBox> elements with no width or height. Unsized, an svg is as wide
// as its container: the question-mark eyebrow filled the whole card, every
// option grew a card-sized tick, and with no column for the option text the
// label ran into its description ("Bankr wallet only0x6be0…"). Read both
// renderers so a class added there fails here until the skin draws it.
describe('desktop ask and plan card skin covers the shared renderer', () => {
  const transcript = resolve(__dirname, '../../../../../../frontend/src/views/chat/transcript')
  const renderer = ['ask.ts', 'plan.ts']
    .map((file) => readFileSync(resolve(transcript, file), 'utf8'))
    .join('\n')
  const emitted = new Set(
    [...renderer.matchAll(/(?<![\w-])chat-(?:ask|plan)(?:-{1,2}[a-z0-9]+)+(?![\w-])/g)].map(
      (m) => m[0],
    ),
  )
  const drawn = (cls: string): boolean => {
    const hook = new RegExp(`\\.${cls}(?![\\w-])`)
    return selectors.some((sel) => hook.test(sel))
  }
  const rule = (selector: string): string | undefined => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    return css.match(
      new RegExp(`^(?:[^{}\\n]+,\\n)*${escaped}(?:,\\n[^{}\\n]+)* \\{[^}]*\\}`, 'm'),
    )?.[0]
  }
  const px = (block: string | undefined, prop: string): number =>
    Number(block?.match(new RegExp(`(?:^|\\s)${prop}: (\\d+(?:\\.\\d+)?)px;`))?.[1])

  it('reads the inventory off the renderers', () => {
    expect(emitted.size).toBeGreaterThan(20)
    for (const cls of [
      'chat-ask-eyebrow',
      'chat-ask-option-check',
      'chat-ask-option-text',
      'chat-ask-header-chip',
      'chat-ask-answered',
      'chat-plan-card',
    ]) {
      expect(emitted, cls).toContain(cls)
    }
  })

  it('has at least one rule for every class the renderers emit', () => {
    const missing = [...emitted].filter((cls) => !drawn(cls))
    expect(missing).toEqual([])
  })

  it('gives every glyph the renderers draw an icon-sized box', () => {
    for (const selector of [
      '.chat-ask-eyebrow svg',
      '.chat-ask-option-check svg',
      '.chat-ask-answered svg',
    ]) {
      const block = rule(selector)
      expect(block, selector).toBeTruthy()
      for (const prop of ['width', 'height']) {
        const size = px(block, prop)
        expect(size, `${selector} ${prop}`).toBeGreaterThan(0)
        expect(size, `${selector} ${prop}`).toBeLessThanOrEqual(16)
      }
    }
    // The tick sits in a fixed box of its own, not the option's full height.
    const check = rule('.chat-ask-option-check')
    expect(px(check, 'width')).toBeLessThanOrEqual(18)
    expect(px(check, 'height')).toBeLessThanOrEqual(18)
    expect(check).toMatch(/flex: none;/)
  })

  it('keeps the label, its description and the header chip apart', () => {
    expect(rule('.chat-ask-option-text')).toMatch(/flex-direction: column;/)
    expect(rule('.chat-ask-eyebrow')).toMatch(/display: flex;[\s\S]*gap: \d+px;/)
    expect(rule('.chat-ask-title')).toMatch(/display: flex;[\s\S]*gap: \d+px;/)
    expect(rule('.chat-ask-answered')).toMatch(/display: flex;[\s\S]*gap: \d+px;/)
  })
})
