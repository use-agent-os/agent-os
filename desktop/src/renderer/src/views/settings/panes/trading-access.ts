import { useQuery } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import { useConnection } from '@/stores/connection'
import type { GatewaySettings } from '@shared/settings'
import { listWithAccess, type WriteAccess } from '~/stores/flows'
import type { FlowSummary } from '~/views/flows/types'
import { errorCode, errorText } from '~/views/trading/logic'

/**
 * Whether the gateway takes trading writes from this window.
 *
 * Trading settings and vault changes are the operator's: the gateway refuses
 * them from any connection it binds to an agent, an anonymous one included
 * (`agent_surface.agent_binding` is set; `config_commit
 * .refuse_any_agent_binding_change`, `rpc_trading._require_operator`). The
 * desktop proves it is the operator with the secret of the gateway it spawned;
 * a gateway it only connects to (External, or one it found running) sees an
 * anonymous connection unless the gateway token was proved from another
 * machine.
 *
 * The gateway reports that same binding before any write in one place: the
 * `writable` flag of `flows.list` is `agent_binding(ctx) is None`
 * (`rpc_flows._access`), the predicate the trading refusal uses. So the pane
 * reads it there, and knows up front. A gateway that does not answer it
 * leaves the state `unknown`, and the first refused write says the same.
 */
export type TradingAccess = 'operator' | 'agent' | 'unknown'

export const TRADING_ACCESS_KEY = ['settings', 'tradingAccess'] as const

export function useTradingAccess(): TradingAccess {
  const rpc = useRpc()
  const connected = useConnection((s) => s.state === 'connected')
  const query = useQuery({
    queryKey: TRADING_ACCESS_KEY,
    enabled: connected,
    retry: false,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      const res = await rpc.call<WriteAccess & { flows?: FlowSummary[] }>('flows.list', {})
      // An older gateway without the flag says nothing either way.
      if (typeof res?.writable !== 'boolean') return 'unknown' as const
      return listWithAccess(res).writable ? ('operator' as const) : ('agent' as const)
    },
  })
  return query.data ?? 'unknown'
}

/** `rpc_trading._require_operator`: a vault write from an agent's connection. */
export const OPERATOR_REQUIRED = 'trading.operator_required'

/**
 * The gateway refused a trading write because of who is asking, not what
 * was asked: the config refusal carries no code, only its wording.
 */
export function isOperatorRefusal(err: unknown): boolean {
  if (errorCode(err) === OPERATOR_REQUIRED) return true
  const text = errorText(err).replace(/[’‘]/g, "'").toLowerCase()
  return text.includes("trading settings are the user's to change")
}

const LOOPBACK = new Set(['localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'])

export function isLoopbackHost(host: string): boolean {
  const h = host.trim().toLowerCase()
  return LOOPBACK.has(h) || h.startsWith('127.') || h.endsWith('.localhost')
}

/**
 * What to tell someone whose window is read-only. A gateway on this Mac
 * only knows the app that started it (a token proves nothing from the same
 * host), so the way out is letting the app run it. A gateway elsewhere takes
 * the operator's writes from a connection that proved its token.
 */
export function readOnlyAdvice(
  gateway: Pick<GatewaySettings, 'mode' | 'host'>,
): 'local' | 'remote' {
  return gateway.mode === 'external' && !isLoopbackHost(gateway.host) ? 'remote' : 'local'
}
