import { describe, expect, it } from 'vitest'
import {
  DEFAULT_GATEWAY_SETTINGS,
  DEFAULT_NOTIFICATION_SETTINGS,
  DEFAULT_QUICK_ASK_SETTINGS,
  gatewayUrl,
  mergeSettings,
  normalizeSettings,
} from './settings'

describe('gateway settings', () => {
  it('reads a file written before the scheme existed as plain http', () => {
    const s = normalizeSettings({ gateway: { mode: 'external', host: 'gw.example.com' } })
    expect(s.gateway).toEqual({
      ...DEFAULT_GATEWAY_SETTINGS,
      mode: 'external',
      host: 'gw.example.com',
      scheme: 'http',
    })
  })

  it('keeps https and drops any other scheme', () => {
    expect(normalizeSettings({ gateway: { scheme: 'https' } }).gateway.scheme).toBe('https')
    expect(normalizeSettings({ gateway: { scheme: 'wss' } }).gateway.scheme).toBe('http')
    expect(normalizeSettings({ gateway: { scheme: 7 } }).gateway.scheme).toBe('http')
  })

  it('builds the base URL from scheme, host and port', () => {
    expect(gatewayUrl(DEFAULT_GATEWAY_SETTINGS)).toBe('http://127.0.0.1:18791')
    expect(
      gatewayUrl({
        ...DEFAULT_GATEWAY_SETTINGS,
        scheme: 'https',
        host: 'gw.example.com',
        port: 443,
      }),
    ).toBe('https://gw.example.com:443')
    expect(gatewayUrl({ ...DEFAULT_GATEWAY_SETTINGS, host: '[::1]' })).toBe('http://[::1]:18791')
  })
})

describe('notification settings', () => {
  it('reads a legacy file with only the three old keys', () => {
    const s = normalizeSettings({
      notifications: { sound: false, replyDone: false, approvals: true },
    })
    expect(s.notifications).toEqual({
      ...DEFAULT_NOTIFICATION_SETTINGS,
      sound: false,
      replyDone: false,
      approvals: true,
    })
  })

  it('rejects values outside the enums and keeps the defaults', () => {
    const s = normalizeSettings({
      notifications: {
        whenActive: 'always',
        jobs: 'some',
        replyMinSeconds: 7,
        soundName: 'Airhorn',
        muteUntil: 'soon',
      },
    })
    expect(s.notifications.whenActive).toBe('banner')
    expect(s.notifications.jobs).toBe('failures')
    expect(s.notifications.replyMinSeconds).toBe(0)
    expect(s.notifications.soundName).toBe('chime')
    expect(s.notifications.muteUntil).toBeNull()
  })

  it('accepts every valid value', () => {
    const s = normalizeSettings({
      notifications: {
        enabled: false,
        whenActive: 'system',
        jobs: 'all',
        replyMinSeconds: 60,
        soundName: 'Glass',
        muteUntil: 1_800_000_000_000,
        badge: false,
        bounce: false,
        preview: false,
      },
    })
    expect(s.notifications).toMatchObject({
      enabled: false,
      whenActive: 'system',
      jobs: 'all',
      replyMinSeconds: 60,
      soundName: 'Glass',
      muteUntil: 1_800_000_000_000,
      badge: false,
      bounce: false,
      preview: false,
    })
  })

  it('patches one key without touching the rest', () => {
    const base = normalizeSettings({})
    const next = mergeSettings(base, { notifications: { muteUntil: 42 } })
    expect(next.notifications.muteUntil).toBe(42)
    expect(next.notifications.soundName).toBe('chime')
    expect(
      mergeSettings(next, { notifications: { muteUntil: null } }).notifications.muteUntil,
    ).toBe(null)
  })
})

describe('general settings', () => {
  it('shows the menu bar item unless the file says otherwise', () => {
    expect(normalizeSettings({}).general.showInMenuBar).toBe(true)
    // A file from before the setting existed keeps the item.
    expect(normalizeSettings({ general: { enterToSend: false } }).general.showInMenuBar).toBe(true)
    expect(normalizeSettings({ general: { showInMenuBar: 'no' } }).general.showInMenuBar).toBe(true)
    expect(normalizeSettings({ general: { showInMenuBar: false } }).general.showInMenuBar).toBe(
      false,
    )
  })

  it('turns the menu bar item off and on through a patch', () => {
    const off = mergeSettings(normalizeSettings({}), { general: { showInMenuBar: false } })
    expect(off.general.showInMenuBar).toBe(false)
    expect(off.general.stopGatewayOnQuit).toBe(true)
    expect(mergeSettings(off, { general: { showInMenuBar: true } }).general.showInMenuBar).toBe(
      true,
    )
  })
})

describe('security settings', () => {
  it('defaults Touch ID to off, for a missing section and an old file alike', () => {
    expect(normalizeSettings({}).security).toEqual({ touchId: 'off' })
    expect(normalizeSettings({ security: null }).security).toEqual({ touchId: 'off' })
    expect(normalizeSettings({ notifications: { sound: false } }).security.touchId).toBe('off')
  })

  it('keeps each valid mode and drops anything else', () => {
    for (const mode of ['off', 'high', 'all'] as const) {
      expect(normalizeSettings({ security: { touchId: mode } }).security.touchId).toBe(mode)
    }
    for (const bad of ['always', 'HIGH', true, 1, null, '']) {
      expect(normalizeSettings({ security: { touchId: bad } }).security.touchId).toBe('off')
    }
    // Unknown keys in the section are not carried along.
    expect(normalizeSettings({ security: { touchId: 'high', pin: '1234' } }).security).toEqual({
      touchId: 'high',
    })
  })

  it('patches the mode without touching another section', () => {
    const base = normalizeSettings({ notifications: { sound: false } })
    const next = mergeSettings(base, { security: { touchId: 'all' } })
    expect(next.security.touchId).toBe('all')
    expect(next.notifications.sound).toBe(false)
    expect(mergeSettings(next, { security: { touchId: 'off' } }).security.touchId).toBe('off')
  })
})

describe('quick ask settings', () => {
  it('defaults to on, on ⌥ Space, for a file written before the section existed', () => {
    const s = normalizeSettings({ general: { enterToSend: false } })
    expect(s.quickAsk).toEqual(DEFAULT_QUICK_ASK_SETTINGS)
    expect(s.quickAsk).toEqual({ enabled: true, shortcut: 'Alt+Space' })
  })

  it('keeps a valid choice from the fixed list', () => {
    const s = normalizeSettings({
      quickAsk: { enabled: false, shortcut: 'CommandOrControl+Shift+Space' },
    })
    expect(s.quickAsk).toEqual({ enabled: false, shortcut: 'CommandOrControl+Shift+Space' })
  })

  it('refuses a free-form accelerator or a non-boolean switch', () => {
    const s = normalizeSettings({
      quickAsk: { enabled: 'yes', shortcut: 'Command+Q', extra: 1 },
    })
    expect(s.quickAsk).toEqual(DEFAULT_QUICK_ASK_SETTINGS)
    expect(normalizeSettings({ quickAsk: 'Alt+Space' }).quickAsk).toEqual(
      DEFAULT_QUICK_ASK_SETTINGS,
    )
  })

  it('merges a one-key patch without touching the rest of the section', () => {
    const base = normalizeSettings({ quickAsk: { enabled: true, shortcut: 'Control+Space' } })
    const next = mergeSettings(base, { quickAsk: { enabled: false } })
    expect(next.quickAsk).toEqual({ enabled: false, shortcut: 'Control+Space' })
    expect(
      mergeSettings(next, { quickAsk: { shortcut: 'Bogus' as never } }).quickAsk.shortcut,
    ).toBe('Alt+Space')
  })
})
