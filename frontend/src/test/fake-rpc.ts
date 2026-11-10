// A scriptable stand-in for WsRpcClient, for the web console's tests.
import type { WsRpcClient, RpcState } from '@/lib/ws-rpc'

type Handler = (params: Record<string, unknown>) => unknown | Promise<unknown>

export class FakeRpc {
  handlers = new Map<string, Handler>()
  calls: { method: string; params: Record<string, unknown> }[] = []
  private listeners = new Map<string, Set<(...args: unknown[]) => void>>()
  state: RpcState = 'connected'
  capabilities: string[] = []
  policy: Record<string, unknown> = {}
  lastFrameTime = Date.now()
  /** WsRpcClient's URL of the last connect(); tests set it to simulate a new gateway. */
  currentUrl = ''

  /** Answer `method` with a value, or with a function of the params. */
  handle(method: string, answer: unknown): this {
    this.handlers.set(
      method,
      typeof answer === 'function' ? (answer as Handler) : () => structuredClone(answer),
    )
    return this
  }

  async call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    this.calls.push({ method, params })
    const handler = this.handlers.get(method)
    if (!handler) throw new Error(`FakeRpc: no handler for ${method}`)
    return (await handler(params)) as T
  }

  callsTo(method: string): Record<string, unknown>[] {
    return this.calls.filter((c) => c.method === method).map((c) => c.params)
  }

  on(event: string, handler: (...args: unknown[]) => void): () => void {
    let set = this.listeners.get(event)
    if (!set) this.listeners.set(event, (set = new Set()))
    set.add(handler)
    return () => set.delete(handler)
  }

  emit(event: string, ...args: unknown[]): void {
    for (const handler of [...(this.listeners.get(event) ?? [])]) handler(...args)
    if (!event.startsWith('_')) {
      for (const handler of [...(this.listeners.get('*') ?? [])]) handler(event, ...args)
    }
  }

  listenerCount(event: string): number {
    return this.listeners.get(event)?.size ?? 0
  }

  setState(state: RpcState): void {
    this.state = state
    this.emit('_state', state)
  }

  hasCapability(name: string): boolean {
    return this.capabilities.includes(name)
  }

  waitForConnection(): Promise<void> {
    return Promise.resolve()
  }

  connect(): void {}
  disconnect(): void {}
  reconnect(): void {}
  setClientInfo(): void {}

  asClient(): WsRpcClient {
    return this as unknown as WsRpcClient
  }
}
