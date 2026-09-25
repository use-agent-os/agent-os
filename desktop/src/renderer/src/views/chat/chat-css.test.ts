import { readFileSync } from 'node:fs'
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
    expect(css).toMatch(/\.chat-jump-dock\[data-visible='false'\] \{[\s\S]*?visibility: hidden;/)
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
