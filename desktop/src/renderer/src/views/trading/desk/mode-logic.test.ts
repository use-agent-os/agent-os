// The stylesheet is read as text, not loaded: this asserts what it says, and
// vitest stubs CSS imports to the empty string, so `?raw` would see nothing.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  deriveMode,
  ENTRANCE_MS,
  LEAVE_MS,
  mintTradingSessionKey,
  redirectTarget,
  shouldPlayEntrance,
  toggleTarget,
} from './mode-logic'

const DESK = 'agent:trading:webchat:trading-abc123'

describe('deriveMode', () => {
  it('is trading for every chat the desk minted, not only the newest', () => {
    expect(deriveMode(DESK)).toBe('trading')
    expect(deriveMode('agent:trading:webchat:trading-older1')).toBe('trading')
    expect(deriveMode(mintTradingSessionKey())).toBe('trading')
  })
  it('is trading for a chat started from the desk project\'s "+" (`sessions.create`)', () => {
    expect(deriveMode('agent:trading:04d17d83')).toBe('trading')
  })
  it('is chat for anything that is not a conversation with the trading agent', () => {
    expect(deriveMode('')).toBe('chat')
    expect(deriveMode(null)).toBe('chat')
    expect(deriveMode('agent:main:webchat:other')).toBe('chat')
    // From before the desk had its own agent: it ran as `main`, and still does.
    expect(deriveMode('agent:main:webchat:trading-old')).toBe('chat')
    // The trading agent's scheduled runs and helpers are not desk chats.
    expect(deriveMode('agent:trading:cron:job1:run:r1')).toBe('chat')
    expect(deriveMode('agent:trading:subagent:s1')).toBe('chat')
    expect(deriveMode('agent:trading:hook:h1')).toBe('chat')
    expect(deriveMode('agent:trading:acp:3473e002')).toBe('chat')
    // A channel routed to the trading agent answers there, not at this desk.
    expect(deriveMode('agent:trading:telegram:direct:1245')).toBe('chat')
    // The same shape under another agent, and an agent merely named like it.
    expect(deriveMode('agent:main:04d17d83')).toBe('chat')
    expect(deriveMode('agent:trading-bot:04d17d83')).toBe('chat')
  })
  it('mints desk keys under the desk agent', () => {
    expect(mintTradingSessionKey()).toMatch(/^agent:trading:webchat:trading-[a-z0-9]+$/)
  })
})

describe('toggleTarget', () => {
  it('enters the desk and remembers the chat it left', () => {
    expect(
      toggleTarget({
        mode: 'chat',
        tradingKey: DESK,
        currentKey: 'agent:main:webchat:x1',
        returnKey: null,
      }),
    ).toEqual({ path: `/sessions/${encodeURIComponent(DESK)}`, remember: 'agent:main:webchat:x1' })
  })
  it('remembers nothing from the keyless home', () => {
    expect(
      toggleTarget({ mode: 'chat', tradingKey: DESK, currentKey: null, returnKey: null }),
    ).toEqual({ path: `/sessions/${encodeURIComponent(DESK)}`, remember: null })
  })
  it('returns to the remembered chat, else home', () => {
    expect(
      toggleTarget({
        mode: 'trading',
        tradingKey: DESK,
        currentKey: DESK,
        returnKey: 'agent:main:webchat:x1',
      }),
    ).toEqual({ path: '/sessions/agent%3Amain%3Awebchat%3Ax1', remember: null })
    expect(
      toggleTarget({ mode: 'trading', tradingKey: DESK, currentKey: DESK, returnKey: DESK }),
    ).toEqual({ path: '/sessions', remember: null })
  })
})

describe('redirectTarget', () => {
  it('keeps the query the notification carried', () => {
    expect(redirectTarget(DESK, '?order=o1')).toBe(`/sessions/${encodeURIComponent(DESK)}?order=o1`)
    expect(redirectTarget(DESK, 'desk=1')).toBe(`/sessions/${encodeURIComponent(DESK)}?desk=1`)
    expect(redirectTarget(DESK, '')).toBe(`/sessions/${encodeURIComponent(DESK)}`)
  })
})

describe('shouldPlayEntrance', () => {
  const base = {
    prevMode: 'chat' as const,
    mode: 'trading' as const,
    reducedMotion: false,
    still: false,
    requested: false,
  }
  it('plays on a switch from chat', () => {
    expect(shouldPlayEntrance(base)).toBe(true)
  })
  it('plays on a fresh mount only when the desk was asked for', () => {
    expect(shouldPlayEntrance({ ...base, prevMode: null })).toBe(false)
    expect(shouldPlayEntrance({ ...base, prevMode: null, requested: true })).toBe(true)
  })
  it('never replays on a re-render, nor under reduced motion or a pending ask', () => {
    expect(shouldPlayEntrance({ ...base, prevMode: 'trading' })).toBe(false)
    expect(shouldPlayEntrance({ ...base, reducedMotion: true })).toBe(false)
    expect(shouldPlayEntrance({ ...base, still: true })).toBe(false)
    expect(shouldPlayEntrance({ ...base, mode: 'chat' })).toBe(false)
  })
})

/* The choreography is written in two places that cannot see each other: the
   beats are CSS custom properties in desk.css, and the clock that clears
   `data-enter` is ENTRANCE_MS here. Lengthening a beat without moving the
   clock cuts the animation off mid-flight and nothing in the app would say
   so, so the contract is asserted against the stylesheet itself. */
describe('the entrance contract', () => {
  const deskCss = readFileSync('src/renderer/src/views/trading/desk/desk.css', 'utf8')
  const ms = (name: string): number => {
    const m = new RegExp(`--${name}:\\s*(\\d+(?:\\.\\d+)?)ms`).exec(deskCss)
    if (!m?.[1]) throw new Error(`desk.css has no --${name}`)
    return Number(m[1])
  }

  it('gives every beat room to finish before data-enter is cleared', () => {
    // The last instrument to land: four of them, one --enter-land-step apart.
    const lastLanding = ms('enter-land-start') + 3 * ms('enter-land-step') + ms('enter-land')
    const beats = [
      // The veil runs the whole sequence so it can lift instead of popping off.
      ms('enter-total'),
      lastLanding,
      ms('enter-count-start') + ms('enter-count'),
      ms('enter-stamp-start') + ms('enter-stamp'),
      ms('enter-pulse-start') + ms('enter-pulse'),
    ]
    // useEntrance clears the attribute at ENTRANCE_MS + 40.
    expect(Math.max(...beats)).toBeLessThanOrEqual(ENTRANCE_MS + 40)
    // …and the clock is not wildly long either: the desk must not sit lit.
    expect(Math.max(...beats)).toBeGreaterThan(ENTRANCE_MS - 200)
  })

  it('keeps the documented total and the leave in step with the stylesheet', () => {
    expect(ms('enter-total')).toBe(ENTRANCE_MS)
    expect(ms('leave-total')).toBe(LEAVE_MS)
  })
})
