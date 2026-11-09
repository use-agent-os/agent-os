import { describe, expect, it } from 'vitest'
import {
  agentOfSessionKey,
  defaultSessionKey,
  pathToSessionKey,
  sessionKeyToPath,
} from './session-url'

describe('session-url codec', () => {
  it('maps a webchat key to /chat/<agent>/<parts>', () => {
    expect(sessionKeyToPath('agent:main:webchat:default')).toBe('/chat/main/webchat/default')
    expect(pathToSessionKey('/chat/main/webchat/default')).toBe('agent:main:webchat:default')
  })

  it('percent-encodes reserved characters inside a part', () => {
    const key = 'agent:main:webchat:a/b%c'
    const path = sessionKeyToPath(key)
    expect(path).toBe('/chat/main/webchat/a%2Fb%25c')
    expect(pathToSessionKey(path)).toBe(key)
  })

  it('uses the reserved ~ form for keys that are not agent keys', () => {
    expect(sessionKeyToPath('cron:job-1')).toBe('/chat/~/cron%3Ajob-1')
    expect(pathToSessionKey('/chat/~/cron%3Ajob-1')).toBe('cron:job-1')
  })

  it('uses the reserved form when the agent part is not normalized', () => {
    for (const key of ['agent:Main:webchat:x', 'agent:a b:webchat:x', 'agent:~:x', 'agent:main']) {
      expect(sessionKeyToPath(key).startsWith('/chat/~/')).toBe(true)
      expect(pathToSessionKey(sessionKeyToPath(key))).toBe(key)
    }
  })

  it('escapes dot-only parts that browsers would normalize away', () => {
    const key = 'agent:main:webchat:..'
    expect(sessionKeyToPath(key)).toBe('/chat/main/webchat/..;')
    expect(pathToSessionKey(sessionKeyToPath(key))).toBe(key)
  })

  it('round-trips generated keys with :, /, %, spaces and unicode', () => {
    const alphabet = [
      'a',
      'Z',
      '0',
      ':',
      '/',
      '%',
      ' ',
      '~',
      '.',
      '?',
      '#',
      'é',
      '日',
      '🙂',
      '+',
      '&',
    ]
    let seed = 42
    const rand = (n: number) => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed % n
    }
    for (let i = 0; i < 500; i++) {
      const len = 1 + rand(12)
      let tail = ''
      for (let j = 0; j < len; j++) tail += alphabet[rand(alphabet.length)]
      const agent = ['main', 'research', 'x_1', 'Bad', ''][rand(5)]
      const key = rand(4) === 0 ? tail : `agent:${agent}:webchat:${tail}`
      const path = sessionKeyToPath(key)
      expect(path.startsWith('/chat/')).toBe(true)
      expect(path.slice(1).split('/').length).toBeGreaterThanOrEqual(3)
      expect(pathToSessionKey(path), JSON.stringify(key)).toBe(key)
      // Every segment survives URL parsing unchanged.
      expect(new URL(path, 'http://h').pathname).toBe(path)
    }
  })

  it('rejects paths that do not name a session', () => {
    expect(pathToSessionKey('/chat')).toBeNull()
    expect(pathToSessionKey('/chat/main')).toBeNull()
    expect(pathToSessionKey('/chat/Main/x')).toBeNull()
    expect(pathToSessionKey('/chat/main/%E0%A4%A')).toBeNull()
    expect(pathToSessionKey('/chat/~/a/b')).toBeNull()
  })

  it('accepts the path without the /chat prefix and with a trailing slash', () => {
    expect(pathToSessionKey('main/webchat/x/')).toBe('agent:main:webchat:x')
  })

  it('exposes agent helpers', () => {
    expect(agentOfSessionKey('agent:research:webchat:x')).toBe('research')
    expect(agentOfSessionKey('cron:1')).toBeNull()
    expect(defaultSessionKey('main')).toBe('agent:main:webchat:default')
  })
})
