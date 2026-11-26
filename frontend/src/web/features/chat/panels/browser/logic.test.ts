import { describe, expect, it } from 'vitest'
import { browserFailure, normalizeAddress, normalizeSnapshot, normalizeStatus } from './logic'

describe('browser address bar', () => {
  it('turns a bare host into an https address', () => {
    expect(normalizeAddress('example.com')).toBe('https://example.com/')
    expect(normalizeAddress('  docs.example.com/a?b=1 ')).toBe('https://docs.example.com/a?b=1')
    expect(normalizeAddress('localhost:8080')).toBe('https://localhost:8080/')
    expect(normalizeAddress('http://example.com/x')).toBe('http://example.com/x')
    expect(normalizeAddress('about:blank')).toBe('about:blank')
  })

  it('refuses words and other schemes', () => {
    for (const text of [
      '',
      'hello world',
      'javascript:alert(1)',
      'file:///etc/passwd',
      'data:x',
      'chrome://settings',
      'nohost',
    ]) {
      expect(normalizeAddress(text)).toBeNull()
    }
  })
})

describe('browser view on the wire', () => {
  it('reads a status with safe defaults', () => {
    expect(normalizeStatus({})).toMatchObject({
      enabled: false,
      installed: false,
      running: false,
      attachConfirmed: true,
      mode: 'managed',
    })
  })

  it('keeps only listed tabs and an inline image it can show', () => {
    const snap = normalizeSnapshot({
      running: true,
      tabs: [
        { tabId: 't1', title: 'One', url: 'https://a.test', active: true },
        { tabId: 'x9', title: 'bad' },
      ],
      activeTabId: 't1',
      image: { mime: 'image/jpeg', data: 'AAAA' },
    })
    expect(snap.tabs.map((t) => t.tabId)).toEqual(['t1'])
    expect(snap.image).toEqual({ mime: 'image/jpeg', data: 'AAAA' })
    expect(normalizeSnapshot({ image: { mime: 'text/html', data: 'AAAA' } }).image).toBeNull()
    expect(normalizeSnapshot({ image: { mime: 'image/png', data: '"><script>' } }).image).toBeNull()
    expect(normalizeSnapshot({ activeTabId: 'javascript:' }).activeTabId).toBeNull()
  })

  it('maps refusals to states', () => {
    expect(browserFailure({ code: 'METHOD_NOT_FOUND' })).toBe('outdated')
    expect(browserFailure({ details: { code: 'browser.not_installed' } })).toBe('disabled')
    expect(browserFailure({ details: { code: 'browser.blocked' } })).toBe('blocked')
    expect(browserFailure(new Error('x'))).toBe('failed')
  })
})
