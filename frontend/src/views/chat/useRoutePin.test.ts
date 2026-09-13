import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useRoutePin } from './useRoutePin'
import type { WsRpcClient } from '@/lib/ws-rpc'

vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

type Handler = (...args: unknown[]) => void

const HOLD_GET_OK = {
  enabled: true,
  provider: 'opencap',
  hold: null,
  tiers: [
    { tier: 'c0', model: 'deepseek-v4-flash' },
    { tier: 'c3', model: 'claude-opus-5' },
  ],
  imageTiers: [{ tier: 'image_model', model: 'gpt-4o' }],
}

const MODELS_OK = [
  { id: 'grok-5', name: 'grok-5', provider: 'opencap' },
  { id: 'claude-opus-5', name: 'claude-opus-5', provider: 'opencap' },
]

function fakeRpc(overrides: Record<string, unknown> = {}, options: { connected?: boolean } = {}) {
  const listeners = new Map<string, Set<Handler>>()
  const calls: { method: string; params: unknown }[] = []
  const responses: Record<string, unknown> = {
    'router.hold.get': HOLD_GET_OK,
    'models.list': MODELS_OK,
    ...overrides,
  }
  // Mirrors the real client: `waitForConnection` resolves at once when the
  // socket is up and otherwise on the next `_state: connected`.
  let connected = options.connected ?? true
  const emit = (event: string, payload: unknown) => {
    if (event === '_state') connected = payload === 'connected'
    listeners.get(event)?.forEach((handler) => handler(payload))
  }
  const rpc = {
    call: vi.fn((method: string, params: unknown) => {
      if (!connected) return Promise.reject(new Error('Not connected'))
      calls.push({ method, params })
      const value = responses[method]
      if (value instanceof Error) return Promise.reject(value)
      return Promise.resolve(value ?? {})
    }),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    waitForConnection: vi.fn(() => {
      if (connected) return Promise.resolve()
      return new Promise<void>((resolve) => {
        const unsub = rpc.on('_state', (state: unknown) => {
          if (state === 'connected') {
            unsub()
            resolve()
          }
        })
      })
    }),
  }
  return { rpc: rpc as unknown as WsRpcClient, calls, emit }
}

describe('useRoutePin', () => {
  it('reads the pin back from the gateway rather than guessing it', async () => {
    const { rpc } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c3' } },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(result.current.pinned).toBe('c3'))
    expect(result.current.enabled).toBe(true)
    expect(result.current.tiers.map((row) => row.tier)).toEqual(['c0', 'c3'])
  })

  it('reports no pin and no tiers when the router is off', async () => {
    const { rpc } = fakeRpc({
      'router.hold.get': { enabled: false, hold: null, tiers: [] },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(rpc.call).toHaveBeenCalled())
    expect(result.current.enabled).toBe(false)
    expect(result.current.pinned).toBeNull()
  })

  it('stays disabled when the read fails instead of surfacing an error', async () => {
    const { rpc } = fakeRpc({ 'router.hold.get': new Error('no such method') })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(rpc.call).toHaveBeenCalled())
    expect(result.current.enabled).toBe(false)
    expect(result.current.tiers).toEqual([])
  })

  it('waits for the socket instead of filing "Not connected" as router off', async () => {
    // The desktop opens onto the home chat while the gateway is still starting,
    // so the first mount happens with no socket. The picker used to stay
    // disabled until the next session switch.
    const { rpc, calls, emit } = fakeRpc({}, { connected: false })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(rpc.waitForConnection).toHaveBeenCalled())
    expect(calls).toHaveLength(0)
    expect(result.current.enabled).toBe(false)

    act(() => emit('_state', 'connected'))

    await waitFor(() => expect(result.current.enabled).toBe(true))
    expect(calls[0]).toEqual({ method: 'router.hold.get', params: { key: 'agent:main:main' } })
    // The first connection is read exactly once: the mount read was waiting on
    // it, and the reconnect listener must not add a second.
    await new Promise((r) => setTimeout(r, 20))
    expect(calls.filter((c) => c.method === 'router.hold.get')).toHaveLength(1)
  })

  it('re-reads the pin after a reconnect, since a restarted gateway drops every pin', async () => {
    const { rpc, calls, emit } = fakeRpc()
    const holdReads = () => calls.filter((c) => c.method === 'router.hold.get')
    renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(holdReads()).toHaveLength(1))

    act(() => emit('_state', 'disconnected'))
    act(() => emit('_state', 'connected'))

    await waitFor(() => expect(holdReads()).toHaveLength(2))
  })

  it('re-reads the pin when the session changes', async () => {
    const { rpc, calls } = fakeRpc()
    const holdReads = () => calls.filter((c) => c.method === 'router.hold.get')
    const { rerender } = renderHook(({ key }) => useRoutePin(rpc, key), {
      initialProps: { key: 'agent:main:one' },
    })
    await waitFor(() => expect(holdReads()).toHaveLength(1))

    rerender({ key: 'agent:main:two' })

    await waitFor(() => expect(holdReads()).toHaveLength(2))
    expect(holdReads()[1]!.params).toEqual({ key: 'agent:main:two' })
  })

  it('does not refetch the model catalog on a session switch', async () => {
    // The catalog is a gateway-wide fact; only the pin is per-session.
    const { rpc, calls } = fakeRpc()
    const { rerender, result } = renderHook(({ key }) => useRoutePin(rpc, key), {
      initialProps: { key: 'agent:main:one' },
    })
    await waitFor(() => expect(result.current.models).toHaveLength(2))

    rerender({ key: 'agent:main:two' })
    await waitFor(() => expect(calls.filter((c) => c.method === 'router.hold.get')).toHaveLength(2))

    expect(calls.filter((c) => c.method === 'models.list')).toHaveLength(1)
  })

  it('does not carry one session’s pin over to the next', async () => {
    // Holds are per-session; a stale label would claim a route the new session
    // is not on. The second read resolves only after the assertion below.
    const { rpc } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c3' } },
    })
    const { result, rerender } = renderHook(({ key }) => useRoutePin(rpc, key), {
      initialProps: { key: 'agent:main:one' },
    })
    await waitFor(() => expect(result.current.pinned).toBe('c3'))

    rerender({ key: 'agent:main:two' })

    expect(result.current.pinned).toBeNull()
  })

  it('tracks the tier the router actually used', async () => {
    const { rpc, emit } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    act(() => emit('session.event.router_decision', { tier: 'c2', source: 'pilot' }))

    expect(result.current.lastRoutedTier).toBe('c2')
    expect(result.current.imageOverride).toBe(false)
  })

  it('keeps the model the decision names, not just the tier', async () => {
    const { rpc, emit } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    act(() =>
      emit('session.event.router_decision', { tier: 'c2', model: 'glm-5.2', source: 'pilot' }),
    )

    expect(result.current.lastRoutedModel).toBe('glm-5.2')
  })

  it('reads the model from an image route, whose tier has no pinnable row', async () => {
    const { rpc, emit } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    // `router.hold.get` reports text tiers only, so `image_model` can never be
    // resolved to a model through `tiers` — the decision is the only source.
    act(() =>
      emit('session.event.router_decision', {
        tier: 'image_model',
        model: 'gpt-4o',
        source: 'image_route',
      }),
    )

    expect(result.current.tiers.some((row) => row.tier === 'image_model')).toBe(false)
    expect(result.current.lastRoutedModel).toBe('gpt-4o')
  })

  it('accepts the persisted routed_model spelling of the decision', async () => {
    const { rpc, emit } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    act(() => emit('session.event.router_decision', { tier: 'c1', routed_model: 'gpt-5.6-luna' }))

    expect(result.current.lastRoutedModel).toBe('gpt-5.6-luna')
  })

  it('leaves the routed model null when the decision carries none', async () => {
    const { rpc, emit } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    act(() => emit('session.event.router_decision', { tier: 'c1', model: '  ', source: 'pilot' }))

    expect(result.current.lastRoutedTier).toBe('c1')
    expect(result.current.lastRoutedModel).toBeNull()
  })

  it('flags an image route as an override of the pin', async () => {
    const { rpc, emit } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c0' } },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.pinned).toBe('c0'))

    act(() => emit('session.event.router_decision', { tier: 'image_model', source: 'image_route' }))

    expect(result.current.imageOverride).toBe(true)
  })

  it('does not call an image route an override while routing is automatic', async () => {
    const { rpc, emit } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    act(() => emit('session.event.router_decision', { tier: 'image_model', source: 'image_route' }))

    // Nothing was pinned, so the image tier overrode nothing.
    expect(result.current.isPinned).toBe(false)
    expect(result.current.lastRoutedTier).toBe('image_model')
    expect(result.current.imageOverride).toBe(false)
  })

  it('drops the override flag when the pin it bypassed is cleared', async () => {
    const { rpc, emit } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c0' } },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.pinned).toBe('c0'))

    act(() => emit('session.event.router_decision', { tier: 'image_model', source: 'image_route' }))
    expect(result.current.imageOverride).toBe(true)

    await act(async () => result.current.clear())

    expect(result.current.imageOverride).toBe(false)
  })

  it('clears the override flag once a text turn routes normally again', async () => {
    const { rpc, emit } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c0' } },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.pinned).toBe('c0'))

    act(() => emit('session.event.router_decision', { tier: 'image_model', source: 'image_route' }))
    act(() => emit('session.event.router_decision', { tier: 'c1', source: 'pilot' }))

    expect(result.current.imageOverride).toBe(false)
  })

  it('pins through router.hold.set and reflects it immediately', async () => {
    const { calls, result } = await (async () => {
      const fake = fakeRpc({ 'router.hold.set': { tier: 'c3', model: 'claude-opus-5' } })
      const hook = renderHook(() => useRoutePin(fake.rpc, 'agent:main:main'))
      await waitFor(() => expect(hook.result.current.enabled).toBe(true))
      return { ...fake, result: hook.result }
    })()

    await act(async () => result.current.pin('c3'))

    expect(calls.some((c) => c.method === 'router.hold.set')).toBe(true)
    expect(result.current.pinned).toBe('c3')
  })

  it('clears through router.hold.clear and returns to Auto', async () => {
    const { rpc, calls } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c3' } },
      'router.hold.clear': { cleared: true },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.pinned).toBe('c3'))

    await act(async () => result.current.clear())

    expect(calls.some((c) => c.method === 'router.hold.clear')).toBe(true)
    expect(result.current.pinned).toBeNull()
  })

  it('lists models scoped to the active provider', async () => {
    const { rpc, calls } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(result.current.models).toHaveLength(2))
    // The provider comes from router.hold.get; routing runs through exactly one
    // provider, so an unfiltered catalog would offer unreachable models.
    const listCall = calls.find((c) => c.method === 'models.list')
    expect(listCall?.params).toEqual({ provider: 'opencap' })
  })

  it('does not fetch models until the active provider is known', async () => {
    const { rpc, calls } = fakeRpc({
      'router.hold.get': { enabled: false, hold: null, tiers: [], provider: '' },
    })
    renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(calls.some((c) => c.method === 'router.hold.get')).toBe(true))
    expect(calls.some((c) => c.method === 'models.list')).toBe(false)
  })

  it('reports isPinned for a model pin, not only for a tier pin', async () => {
    // The router-fx strip is suppressed off this flag; a consumer that checked
    // only `pinned` kept animating over a pinned model.
    const { rpc } = fakeRpc({
      'router.hold.get': {
        ...HOLD_GET_OK,
        hold: { tier: 'c1', model: 'grok-5', targetType: 'model' },
      },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(result.current.isPinned).toBe(true))
    expect(result.current.pinned).toBeNull()
  })

  it('reports isPinned for a tier pin', async () => {
    const { rpc } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c3', targetType: 'tier' } },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(result.current.isPinned).toBe(true))
  })

  it('reports isPinned false when routing is automatic', async () => {
    const { rpc } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(result.current.enabled).toBe(true))
    expect(result.current.isPinned).toBe(false)
  })

  it('reads a model pin back as a model, not as its host tier', async () => {
    // A model pin still reports the tier hosting it; only targetType says which
    // the user actually chose.
    const { rpc } = fakeRpc({
      'router.hold.get': {
        ...HOLD_GET_OK,
        hold: { tier: 'c1', model: 'grok-5', targetType: 'model' },
      },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(result.current.pinnedModel).toBe('grok-5'))
    expect(result.current.pinned).toBeNull()
  })

  it('pins a model through router.hold.set and drops any tier pin', async () => {
    const { rpc, calls } = fakeRpc({
      'router.hold.get': { ...HOLD_GET_OK, hold: { tier: 'c3', targetType: 'tier' } },
      'router.hold.set': { tier: 'c1', model: 'grok-5', targetType: 'model' },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.pinned).toBe('c3'))

    await act(async () => result.current.pinModel('grok-5'))

    expect(calls.some((c) => c.method === 'router.hold.set')).toBe(true)
    expect(result.current.pinnedModel).toBe('grok-5')
    expect(result.current.pinned).toBeNull()
  })

  it('clears both a tier pin and a model pin', async () => {
    const { rpc } = fakeRpc({
      'router.hold.get': {
        ...HOLD_GET_OK,
        hold: { tier: 'c1', model: 'grok-5', targetType: 'model' },
      },
      'router.hold.clear': { cleared: true },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.pinnedModel).toBe('grok-5'))

    await act(async () => result.current.clear())

    expect(result.current.pinnedModel).toBeNull()
    expect(result.current.pinned).toBeNull()
  })

  it('keeps the tier rows usable when the catalog fetch fails', async () => {
    const { rpc } = fakeRpc({ 'models.list': new Error('catalog down') })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))

    await waitFor(() => expect(result.current.tiers).toHaveLength(2))
    expect(result.current.models).toEqual([])
    expect(result.current.enabled).toBe(true)
  })

  it('falls back to the config tier list before the first read lands', () => {
    const { rpc } = fakeRpc()
    const { result } = renderHook(() =>
      useRoutePin(rpc, 'agent:main:main', {
        c1: { model: 'gpt-5.6-luna', supportsImage: false, imageOnly: false },
        image_model: { model: 'minimax-m3', supportsImage: true, imageOnly: true },
      }),
    )

    // image_only tiers are not pinnable text routes and must not be offered.
    expect(result.current.tiers).toEqual([{ tier: 'c1', model: 'gpt-5.6-luna' }])
  })

  it('reads the image tiers the gateway reports', async () => {
    const { rpc } = fakeRpc()
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    expect(result.current.imageTiers).toEqual([{ tier: 'image_model', model: 'gpt-4o' }])
    // Kept apart from the pinnable list: a hold on a vision tier never applies.
    expect(result.current.tiers.some((row) => row.tier === 'image_model')).toBe(false)
  })

  it('reports no image tiers when an older gateway omits the field', async () => {
    const { rpc } = fakeRpc({
      'router.hold.get': {
        enabled: true,
        provider: 'opencap',
        hold: null,
        tiers: [{ tier: 'c0', model: 'deepseek-v4-flash' }],
      },
    })
    const { result } = renderHook(() => useRoutePin(rpc, 'agent:main:main'))
    await waitFor(() => expect(result.current.enabled).toBe(true))

    expect(result.current.imageTiers).toEqual([])
  })

  it('falls back to every vision-capable config tier, not only the image-only one', () => {
    const { rpc } = fakeRpc()
    const { result } = renderHook(() =>
      useRoutePin(rpc, 'agent:main:main', {
        c1: { model: 'gpt-5.6-luna', supportsImage: false, imageOnly: false },
        c3: { model: 'claude-opus-5', supportsImage: true, imageOnly: false },
        image_model: { model: 'minimax-m3', supportsImage: true, imageOnly: true },
      }),
    )

    // The router's image branch picks among every supports_image tier, so the
    // fallback splits on that flag rather than on image_only.
    expect(result.current.imageTiers).toEqual([
      { tier: 'c3', model: 'claude-opus-5' },
      { tier: 'image_model', model: 'minimax-m3' },
    ])
  })

  it('does not carry one session’s image tiers over to the next', async () => {
    const { rpc } = fakeRpc()
    const { result, rerender } = renderHook(({ key }) => useRoutePin(rpc, key), {
      initialProps: { key: 'agent:main:one' },
    })
    await waitFor(() => expect(result.current.imageTiers).toHaveLength(1))

    rerender({ key: 'agent:main:two' })

    expect(result.current.imageTiers).toEqual([])
  })
})
