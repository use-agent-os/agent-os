import { waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { resetSession } from './resetSession'

vi.mock('sonner', () => ({
  toast: {
    info: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
  },
}))

function makeRpc() {
  return {
    call: vi.fn<(...args: [string, Record<string, unknown>?]) => Promise<unknown>>(),
  }
}

describe('resetSession', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uses the safe reset path when transcript flushing is available', async () => {
    const rpc = makeRpc()
    rpc.call.mockResolvedValue({})

    await resetSession(rpc, 'agent:main:webchat:default')

    expect(rpc.call).toHaveBeenCalledWith('sessions.reset', {
      key: 'agent:main:webchat:default',
    })
    expect(toast.info).toHaveBeenCalledWith('Session reset')
    expect(toast.warning).not.toHaveBeenCalled()
  })

  it('offers a destructive recovery action when transcript backup is unavailable', async () => {
    const rpc = makeRpc()
    rpc.call
      .mockRejectedValueOnce(
        Object.assign(new Error('backend force instruction must not be shown'), {
          code: 'flush_unavailable',
        }),
      )
      .mockResolvedValueOnce({})

    await resetSession(rpc, 'agent:main:webchat:default')

    expect(toast.error).not.toHaveBeenCalled()
    expect(toast.warning).toHaveBeenCalledWith(
      'Transcript backup is unavailable.',
      expect.objectContaining({
        description: 'Discard the current transcript and reset this session?',
        duration: Infinity,
      }),
    )

    const options = vi.mocked(toast.warning).mock.calls[0]?.[1] as
      { action?: { label?: string; onClick?: () => void } } | undefined
    expect(options?.action?.label).toBe('Discard & reset')
    options?.action?.onClick?.()

    await waitFor(() =>
      expect(rpc.call).toHaveBeenLastCalledWith('sessions.reset', {
        key: 'agent:main:webchat:default',
        force: true,
      }),
    )
    expect(toast.info).toHaveBeenCalledWith('Session reset without transcript backup')
  })
})
