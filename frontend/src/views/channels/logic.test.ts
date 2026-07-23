import { describe, expect, it } from 'vitest'
import {
  channelDisplay,
  channelStats,
  inactiveHint,
  isAccessLocked,
  mergeChannels,
  needsAttention,
  resolveAccessMode,
  senderLabel,
  senderMeta,
  sortChannels,
  statusHint,
  type ChannelAccess,
  type MergedChannel,
  type RawChannel,
} from './logic'

describe('mergeChannels (channels.js:85-95)', () => {
  it('drops channels explicitly configured:false', () => {
    const channels: RawChannel[] = [
      { name: 'a', configured: true },
      { name: 'b', configured: false },
      { name: 'c' }, // undefined configured is kept
    ]
    const merged = mergeChannels(channels, [])
    expect(merged.map((c) => c.name)).toEqual(['a', 'c'])
  })

  it('attaches the access entry by channel name', () => {
    const access: ChannelAccess[] = [{ name: 'a', mode: 'pairing' }]
    const merged = mergeChannels([{ name: 'a' }, { name: 'b' }], access)
    expect(merged[0]!.access).toEqual({ name: 'a', mode: 'pairing' })
    expect(merged[1]!.access).toBeNull()
  })

  it('tolerates undefined inputs', () => {
    expect(mergeChannels(undefined, undefined)).toEqual([])
  })
})

describe('sortChannels (channels.js:96-103)', () => {
  const withPending = (n: number): ChannelAccess => ({
    pending: Array.from({ length: n }, () => ({})),
  })

  it('orders channels with more pending access first', () => {
    const channels: MergedChannel[] = [
      { name: 'none', status: 'running', access: null },
      { name: 'two', status: 'disabled', access: withPending(2) },
      { name: 'one', status: 'running', access: withPending(1) },
    ]
    expect(sortChannels(channels).map((c) => c.name)).toEqual(['two', 'one', 'none'])
  })

  it('breaks ties by status urgency (running < dead < stopped < disabled)', () => {
    const channels: MergedChannel[] = [
      { name: 'disabled', status: 'disabled', access: null },
      { name: 'stopped', status: 'stopped', access: null },
      { name: 'dead', status: 'dead', access: null },
      { name: 'running', status: 'running', access: null },
    ]
    expect(sortChannels(channels).map((c) => c.name)).toEqual([
      'running',
      'dead',
      'stopped',
      'disabled',
    ])
  })

  it('treats an unknown status as urgency 1', () => {
    const channels: MergedChannel[] = [
      { name: 'stopped', status: 'stopped', access: null },
      { name: 'weird', status: 'mystery', access: null },
      { name: 'running', status: 'running', access: null },
    ]
    // running(0) < weird(1) < stopped(2)
    expect(sortChannels(channels).map((c) => c.name)).toEqual(['running', 'weird', 'stopped'])
  })

  it('does not mutate the input array', () => {
    const channels: MergedChannel[] = [
      { name: 'a', status: 'disabled', access: null },
      { name: 'b', status: 'running', access: null },
    ]
    const copy = [...channels]
    sortChannels(channels)
    expect(channels).toEqual(copy)
  })
})

describe('needsAttention (channels.js:398-400)', () => {
  it('is true for dead/restarting/exhausted', () => {
    expect(needsAttention('dead')).toBe(true)
    expect(needsAttention('restarting')).toBe(true)
    expect(needsAttention('exhausted')).toBe(true)
  })
  it('is false otherwise', () => {
    expect(needsAttention('running')).toBe(false)
    expect(needsAttention('stopped')).toBe(false)
    expect(needsAttention(undefined)).toBe(false)
  })
})

describe('channelStats (channels.js:113-121)', () => {
  it('counts connected/attention/inactive/disabled, sums restarts + pending, counts types', () => {
    const channels: MergedChannel[] = [
      { status: 'running', type: 'telegram', restart_attempts: 2, access: { pending: [{}, {}] } },
      { status: 'connected', type: 'discord', restart_attempts: 1, access: null },
      { status: 'dead', type: 'telegram', restart_attempts: 3, access: null },
      { status: 'disabled', type: 'slack', access: null },
      { status: 'stopped', type: 'slack', access: null },
    ]
    const stats = channelStats(channels)
    expect(stats.total).toBe(5)
    expect(stats.connected).toBe(2) // running + connected
    expect(stats.attention).toBe(1) // dead
    expect(stats.inactive).toBe(2) // 5 - 2 - 1
    expect(stats.disabled).toBe(1)
    expect(stats.restarts).toBe(6) // 2 + 1 + 3
    expect(stats.pendingAccess).toBe(2)
    expect(stats.typeCount).toBe(3) // telegram, discord, slack
  })

  it('handles an empty channel list', () => {
    const stats = channelStats([])
    expect(stats).toEqual({
      total: 0,
      connected: 0,
      attention: 0,
      inactive: 0,
      disabled: 0,
      restarts: 0,
      pendingAccess: 0,
      typeCount: 0,
    })
  })
})

describe('inactiveHint (channels.js:402-406)', () => {
  it('reports no inactive channels', () => {
    expect(inactiveHint(0, 0)).toBe('no inactive channels')
  })
  it('reports the disabled count when some are disabled', () => {
    expect(inactiveHint(3, 2)).toBe('2 disabled')
  })
  it('falls back to configured-but-idle', () => {
    expect(inactiveHint(3, 0)).toBe('configured but idle')
  })
})

describe('channelDisplay (channels.js:203-217)', () => {
  it('resolves running/connected to the ok tone', () => {
    const d = channelDisplay({ name: 'tg', status: 'running', access: null })
    expect(d.status).toBe('running')
    expect(d.isRunning).toBe(true)
    expect(d.tone).toBe('ok')
  })

  it('resolves dead to the danger tone', () => {
    const d = channelDisplay({ name: 'tg', status: 'dead', access: null })
    expect(d.isDead).toBe(true)
    expect(d.tone).toBe('danger')
  })

  it('falls back to connected when status is absent but connected:true', () => {
    const d = channelDisplay({ name: 'tg', connected: true, access: null })
    expect(d.status).toBe('connected')
    expect(d.tone).toBe('ok')
  })

  it('falls back to stopped (off tone) when neither status nor connected', () => {
    const d = channelDisplay({ name: 'tg', access: null })
    expect(d.status).toBe('stopped')
    expect(d.tone).toBe('off')
  })

  it('uses id then Unknown for the name, and defaults attempts to 0', () => {
    expect(channelDisplay({ id: 'chan-1', access: null }).name).toBe('chan-1')
    expect(channelDisplay({ access: null }).name).toBe('Unknown')
    expect(channelDisplay({ name: 'tg', access: null }).attempts).toBe('0')
    expect(channelDisplay({ name: 'tg', restart_attempts: 4, access: null }).attempts).toBe('4')
  })

  it('pretty-prints the adapter config as JSON', () => {
    const d = channelDisplay({ name: 'tg', type: 'telegram', access: null })
    expect(d.configJson).toContain('"name": "tg"')
    expect(d.configJson).toContain('"type": "telegram"')
  })
})

describe('statusHint (channels.js:388-396)', () => {
  const base = { status: 'running', isRunning: true, isDead: false, enabled: true, name: 'tg' }
  it('flags a disabled channel first regardless of status', () => {
    expect(statusHint({ ...base, enabled: false })).toContain('Disabled in config')
  })
  it('points a dead adapter at the restart command with the channel name', () => {
    expect(statusHint({ ...base, isRunning: false, isDead: true, status: 'dead' })).toBe(
      'Adapter is dead. Inspect gateway logs, then `agentos channels restart tg`.',
    )
  })
  it('reports a live adapter', () => {
    expect(statusHint(base)).toBe('Adapter is live in the current gateway process.')
  })
  it('reports a restarting adapter', () => {
    expect(statusHint({ ...base, isRunning: false, status: 'restarting' })).toContain('restarting')
  })
  it('points an exhausted adapter at the restart command', () => {
    expect(statusHint({ ...base, isRunning: false, status: 'exhausted' })).toContain(
      'agentos channels restart tg',
    )
  })
  it('falls back to configured-but-not-active', () => {
    expect(statusHint({ ...base, isRunning: false, status: 'stopped' })).toContain(
      'not active in this gateway process',
    )
  })
})

describe('resolveAccessMode (channels.js:251-252)', () => {
  it('passes through valid modes', () => {
    for (const mode of ['pairing', 'allowlist', 'open', 'disabled']) {
      expect(resolveAccessMode(mode)).toBe(mode)
    }
  })
  it('defaults invalid/absent modes to pairing', () => {
    expect(resolveAccessMode('bogus')).toBe('pairing')
    expect(resolveAccessMode(undefined)).toBe('pairing')
  })
})

describe('isAccessLocked (channels.js:253)', () => {
  const now = 1_000_000_000_000 // fixed clock (ms)
  it('is true when locked_until*1000 is in the future', () => {
    expect(isAccessLocked(now / 1000 + 3600, now)).toBe(true)
  })
  it('is false for a past or zero lock', () => {
    expect(isAccessLocked(now / 1000 - 3600, now)).toBe(false)
    expect(isAccessLocked(0, now)).toBe(false)
    expect(isAccessLocked(undefined, now)).toBe(false)
  })
})

describe('senderLabel (channels.js:373-377)', () => {
  it('prefers @username', () => {
    expect(senderLabel({ username: 'ada', display_name: 'Ada L', sender_id: 7 })).toBe('@ada')
  })
  it('falls back to display_name', () => {
    expect(senderLabel({ display_name: 'Ada L', sender_id: 7 })).toBe('Ada L')
  })
  it('falls back to the Telegram user id', () => {
    expect(senderLabel({ sender_id: 7 })).toBe('Telegram user 7')
    expect(senderLabel({})).toBe('Telegram user unknown')
  })
})

describe('senderMeta (channels.js:379-386)', () => {
  it('joins the identity bits with " · " and drops a duplicate display_name', () => {
    // username label is "@ada", so display_name "Ada L" is distinct → included.
    const meta = senderMeta({ username: 'ada', display_name: 'Ada L', sender_id: 7, source: 'dm' })
    expect(meta).toBe('Ada L · ID 7 · dm')
  })
  it('omits display_name when it equals the primary label', () => {
    // no username → label IS the display_name → not duplicated in meta.
    const meta = senderMeta({ display_name: 'Ada L', sender_id: 7 })
    expect(meta).toBe('ID 7')
  })
  it('includes the expiry as a locale time when present', () => {
    const meta = senderMeta({ sender_id: 7, expires_at: 1_700_000_000 })
    expect(meta).toContain('ID 7')
    expect(meta).toContain('expires ')
  })
})
