import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { useSlashCommands } from './useSlashCommands'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const CATALOG = [
  {
    name: '/reset',
    usage: '/reset',
    description: 'Reset',
    aliases: [],
    execution: { action: 'reset_session' },
  },
  {
    name: '/usage',
    usage: '/usage',
    description: 'Usage',
    aliases: [],
    execution: { action: 'usage_status' },
  },
  {
    name: '/c3',
    usage: '/c3',
    description: 'Pin router c3',
    aliases: [],
    execution: { action: 'router.hold.set' },
  },
  {
    name: '/new',
    usage: '/new',
    description: 'New chat',
    aliases: [],
    execution: { action: 'new_chat' },
  },
]

function makeRpc(catalog: unknown[] = CATALOG, reject = false) {
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn((method: string) => {
      if (method === 'commands.list_for_surface') {
        return reject
          ? Promise.reject(new Error('catalog down'))
          : Promise.resolve({ surface: 'web_chat', commands: catalog })
      }
      if (method === 'usage.status') return Promise.resolve({ totals: { tokens: 1234 } })
      if (method === 'router.hold.set') return Promise.resolve({ model: 'glm-4.6' })
      return Promise.resolve({})
    }),
    on: vi.fn((): (() => void) => () => {}),
  }
}
let mockRpc = makeRpc()

/** An RPC whose `commands.list_for_surface` call hangs until `resolve()` is
 * invoked — used to simulate `execute` racing ahead of the mount effect's
 * catalog load (chat.js:2843 `if (!_slashCatalogLoaded) await
 * _loadSlashCommands();`). */
function makeDeferredRpc(catalog: unknown[] = CATALOG) {
  let resolveCatalog!: () => void
  const rpc = {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn((method: string) => {
      if (method === 'commands.list_for_surface') {
        return new Promise((resolve) => {
          resolveCatalog = () => resolve({ surface: 'web_chat', commands: catalog })
        })
      }
      if (method === 'sessions.reset') return Promise.resolve({})
      return Promise.resolve({})
    }),
    on: vi.fn((): (() => void) => () => {}),
  }
  return { rpc, resolveCatalog: () => resolveCatalog() }
}

vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
}))

describe('useSlashCommands', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    vi.mocked(toast.info).mockClear()
    vi.mocked(toast.warning).mockClear()
    vi.mocked(toast.error).mockClear()
  })

  it('loads the catalog via commands.list_for_surface with surface: web_chat (chat.js:2619)', async () => {
    const { result } = renderHook(() => useSlashCommands({ sessionKey: 'k' }))
    await waitFor(() => expect(result.current.commands.length).toBe(4))
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
    expect(mockRpc.call).toHaveBeenCalledWith('commands.list_for_surface', { surface: 'web_chat' })
  })

  it('execute("/reset") calls sessions.reset with the session key (chat.js:2723)', async () => {
    const { result } = renderHook(() =>
      useSlashCommands({ sessionKey: 'agent:main:webchat:default' }),
    )
    await waitFor(() => expect(result.current.commands.length).toBe(4))
    await act(async () => {
      expect(await result.current.execute('/reset')).toBe(true)
    })
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.reset', {
        key: 'agent:main:webchat:default',
      }),
    )
  })

  it('execute("/usage") calls usage.status and toasts the token count (chat.js:2772)', async () => {
    const { result } = renderHook(() => useSlashCommands({ sessionKey: 'k' }))
    await waitFor(() => expect(result.current.commands.length).toBe(4))
    act(() => {
      result.current.execute('/usage')
    })
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('usage.status'))
    await waitFor(() => expect(toast.info).toHaveBeenCalled())
  })

  it('execute("/c3") pins the router tier via router.hold.set (chat.js:2822)', async () => {
    const { result } = renderHook(() => useSlashCommands({ sessionKey: 'k' }))
    await waitFor(() => expect(result.current.commands.length).toBe(4))
    act(() => {
      result.current.execute('/c3')
    })
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('router.hold.set', { key: 'k', tier: 'c3' }),
    )
  })

  it('execute("/new") delegates to onSessionAction (chat.js:2692 session-swap seam)', async () => {
    const onSessionAction = vi.fn()
    const { result } = renderHook(() => useSlashCommands({ sessionKey: 'k', onSessionAction }))
    await waitFor(() => expect(result.current.commands.length).toBe(4))
    act(() => {
      result.current.execute('/new')
    })
    expect(onSessionAction).toHaveBeenCalledWith('new_chat', expect.anything(), '')
  })

  it('execute("/typo") toasts an unsupported-command warning and still returns true (chat.js:2848)', async () => {
    const { result } = renderHook(() => useSlashCommands({ sessionKey: 'k' }))
    await waitFor(() => expect(result.current.commands.length).toBe(4))
    let handled = false
    await act(async () => {
      handled = await result.current.execute('/typo')
    })
    expect(handled).toBe(true)
    expect(toast.warning).toHaveBeenCalledWith('Unsupported command: /typo')
  })

  it('execute() before the catalog resolves awaits the load then still runs the command (chat.js:2843 lazy-load guard)', async () => {
    const { rpc, resolveCatalog } = makeDeferredRpc()
    mockRpc = rpc
    const { result } = renderHook(() =>
      useSlashCommands({ sessionKey: 'agent:main:webchat:default' }),
    )
    // The catalog RPC is in flight; the mount effect's load has not resolved.
    expect(result.current.commands).toEqual([])
    // Wait for the mount effect's `loadCatalog()` to get past `waitForConnection`
    // and actually issue the (still-pending) `commands.list_for_surface` call.
    await waitFor(() =>
      expect(
        mockRpc.call.mock.calls.filter(([m]: [string]) => m === 'commands.list_for_surface'),
      ).toHaveLength(1),
    )

    // Submit "/reset" before the catalog has loaded — legacy still executes
    // it (chat.js:2843 `if (!_slashCatalogLoaded) await _loadSlashCommands()`)
    // rather than toasting "Unsupported command".
    let executePromise!: Promise<boolean>
    act(() => {
      executePromise = result.current.execute('/reset')
    })

    // Still only ONE catalog RPC — the mount effect and `execute` share the
    // same in-flight promise rather than each triggering a separate load.
    expect(
      mockRpc.call.mock.calls.filter(([m]: [string]) => m === 'commands.list_for_surface'),
    ).toHaveLength(1)

    resolveCatalog()
    const handled = await executePromise
    expect(handled).toBe(true)
    expect(toast.warning).not.toHaveBeenCalled()
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('sessions.reset', {
        key: 'agent:main:webchat:default',
      }),
    )
  })

  it('survives a catalog RPC failure with an empty catalog (chat.js:2630 catch)', async () => {
    mockRpc = makeRpc(CATALOG, true)
    const { result } = renderHook(() => useSlashCommands({ sessionKey: 'k' }))
    // The load rejects → catalog stays empty; no throw.
    await act(async () => {})
    expect(result.current.commands).toEqual([])
  })
})
