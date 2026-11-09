import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { apiUrl } from '@/lib/api-origin'
import { authenticatedHeaders } from '@/lib/http-auth'
import {
  approvalMonitor,
  isApprovalBypassMode,
  setBrowserElevated,
  useApprovals,
} from '@/services/approval-monitor'
import {
  effectiveElevatedMode,
  findSessionUsage,
  normalizeSessionUsage,
  type SessionUsage,
} from '@/views/chat/logic'

// The composer's capability state, read and written through the same RPCs and
// endpoints the previous console used (plan.mode.*, /api/elevated-mode,
// config.patch.safe for the Pilot Router, usage.status for the readout).

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function usePlanMode(sessionKey: string) {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['plan.mode.get', sessionKey],
    queryFn: async () => {
      await rpc.waitForConnection()
      const res = await rpc.call<{ planMode?: boolean }>('plan.mode.get', { key: sessionKey })
      return Boolean(res?.planMode)
    },
    enabled: Boolean(sessionKey),
    retry: false,
  })
  const set = useCallback(
    async (on: boolean) => {
      try {
        await rpc.call('plan.mode.set', { key: sessionKey, mode: on ? 'on' : 'off' })
      } catch (e) {
        toast.error(errorMessage(e))
      }
      await queryClient.invalidateQueries({ queryKey: ['plan.mode.get', sessionKey] })
    },
    [rpc, sessionKey, queryClient],
  )
  return { on: query.data ?? false, set }
}

interface ConfigGet {
  permissions?: { default_mode?: string }
  agentos_router?: { enabled?: boolean }
}

export function useChatConfig() {
  const rpc = useRpc()
  return useQuery({
    queryKey: ['config.get', 'chat-composer'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<ConfigGet>('config.get')
    },
    retry: false,
    staleTime: 30_000,
  })
}

export function usePilotRouter() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const config = useChatConfig()
  const [pending, setPending] = useState<boolean | null>(null)
  const enabled = pending ?? Boolean(config.data?.agentos_router?.enabled)
  const set = useCallback(
    async (next: boolean) => {
      setPending(next)
      try {
        await rpc.call('config.patch.safe', {
          patches: {
            'agentos_router.enabled': next,
            'agentos_router.rollout_phase': next ? 'full' : 'observe',
          },
        })
        await queryClient.invalidateQueries({ queryKey: ['config.get'] })
      } catch (e) {
        toast.error(errorMessage(e))
      } finally {
        setPending(null)
      }
    },
    [rpc, queryClient],
  )
  return { enabled, set }
}

export function useElevated(sessionKey: string) {
  const sessionMode = useApprovals((s) => s.elevatedMode)
  const config = useChatConfig()
  const globalMode = config.data?.permissions?.default_mode ?? ''
  const [unavailable, setUnavailable] = useState(false)
  const bypass = isApprovalBypassMode(effectiveElevatedMode(sessionMode, globalMode))
  const set = useCallback(
    async (on: boolean) => {
      const mode = setBrowserElevated(on ? 'bypass' : '')
      useApprovals.getState().setElevatedMode(mode)
      try {
        const resp = await fetch(apiUrl('/api/elevated-mode'), {
          method: 'POST',
          headers: authenticatedHeaders({ 'Content-Type': 'application/json' }),
          credentials: 'same-origin',
          body: JSON.stringify({ sessionKey, mode: mode || 'off' }),
        })
        if (resp.status === 403) {
          setUnavailable(true)
          setBrowserElevated('')
          useApprovals.getState().setElevatedMode('')
          return
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        const payload = (await resp.json().catch(() => ({}))) as { resolvedPending?: boolean }
        if (payload?.resolvedPending) void approvalMonitor.pollNow()
      } catch (e) {
        toast.error(errorMessage(e))
      }
    },
    [sessionKey],
  )
  return { bypass, unavailable, set }
}

export function useSessionUsage(sessionKey: string) {
  const rpc = useRpc()
  return useQuery<SessionUsage | null>({
    queryKey: ['usage.status', sessionKey],
    queryFn: async () => {
      await rpc.waitForConnection()
      const usage = await rpc.call<{ sessions?: never[] }>('usage.status', { sessionKey })
      const row = findSessionUsage(usage?.sessions, sessionKey)
      return row ? normalizeSessionUsage(row) : null
    },
    enabled: Boolean(sessionKey),
    retry: false,
  })
}
