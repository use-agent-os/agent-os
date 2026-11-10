import '@/test/web'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FakeRpc } from '@/test/fake-rpc'
import { exportSessionMarkdown } from './session-actions'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('exportSessionMarkdown', () => {
  it('writes user messages without the gateway time prefix', async () => {
    const written: string[] = []
    vi.stubGlobal(
      'Blob',
      class {
        constructor(parts: string[]) {
          written.push(parts.join(''))
        }
      },
    )
    vi.stubGlobal(
      'URL',
      Object.assign(class extends URL {}, {
        createObjectURL: () => 'blob:export',
        revokeObjectURL: () => {},
      }),
    )
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const rpc = new FakeRpc().handle('chat.history', {
      messages: [
        {
          role: 'user',
          text: '[2026-11-10T11:23+07:00 Tue Asia/Ho_Chi_Minh]\nhello there',
          timestamp: 1,
        },
        { role: 'assistant', text: '[not a prefix] reply', timestamp: 2 },
      ],
    })
    await exportSessionMarkdown(rpc, 'agent:main:webchat:a')
    const md = written[0]!
    expect(md).toContain('hello there')
    expect(md).not.toContain('2026-11-10T11:23')
    expect(md).toContain('[not a prefix] reply')
  })
})
