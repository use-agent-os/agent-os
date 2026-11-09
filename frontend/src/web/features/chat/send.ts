import { callIdempotent, isTransportError, newIdempotencyKey, type WsRpcClient } from '@/lib/ws-rpc'

// Sending through a reconnect (connection spec "Reconnect grace period"): a
// message is delivered once after the socket comes back, or reported as not
// sent with a Retry that reuses the same idempotency key, so a gateway that did
// receive the first attempt answers with that result instead of acting twice.
// It is never lost silently.

export type SendOutcome<T> =
  | { status: 'sent'; result: T }
  | { status: 'not_sent'; error: unknown; retry: () => Promise<SendOutcome<T>> }
  | { status: 'failed'; error: unknown }

export async function sendOnce<T = unknown>(
  rpc: Pick<WsRpcClient, 'call' | 'waitForConnection'>,
  method: string,
  params: Record<string, unknown>,
  opts: { reconnectTimeoutMs?: number } = {},
): Promise<SendOutcome<T>> {
  const request = {
    ...params,
    idempotency_key:
      typeof params.idempotency_key === 'string' && params.idempotency_key
        ? params.idempotency_key
        : newIdempotencyKey(),
  }
  try {
    const result = await callIdempotent<T>(rpc, method, request, opts)
    return { status: 'sent', result }
  } catch (error) {
    // The gateway answered with an error: that is a failure, not "not sent".
    if (!isTransportError(error)) return { status: 'failed', error }
    return { status: 'not_sent', error, retry: () => sendOnce<T>(rpc, method, request, opts) }
  }
}
