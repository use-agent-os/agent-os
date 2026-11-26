import { useQuery } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import {
  DOWNLOAD_METHOD,
  FILES_QUERY_ROOT,
  LIST_METHOD,
  normalizeListing,
  normalizeRead,
  normalizeTicket,
  READ_METHOD,
  type Listing,
  type ReadResult,
  type TicketResult,
} from '@/lib/workspace-files'

/** One directory of an agent's workspace. `''` is the root. */
export function useListing(agentId: string, path: string, enabled = true) {
  const rpc = useRpc()
  return useQuery<Listing>({
    queryKey: [FILES_QUERY_ROOT, agentId, 'list', path],
    queryFn: async () => {
      await rpc.waitForConnection()
      return normalizeListing(await rpc.call<unknown>(LIST_METHOD, { path, agentId }), path)
    },
    enabled,
    retry: false,
    staleTime: 15_000,
  })
}

/** A file's text, or the reason there is none (`binary`, `download`). */
export function useFileText(agentId: string, path: string | null, enabled: boolean) {
  const rpc = useRpc()
  return useQuery<ReadResult>({
    queryKey: [FILES_QUERY_ROOT, agentId, 'read', path],
    queryFn: async () => {
      await rpc.waitForConnection()
      return normalizeRead(await rpc.call<unknown>(READ_METHOD, { path, agentId }), path ?? '')
    },
    enabled: enabled && path !== null,
    retry: false,
    staleTime: 15_000,
    // A megabyte of text per file is not worth keeping once another is open.
    gcTime: 30_000,
  })
}

/** How long a ticket is reused: under the gateway's five-minute lifetime. */
const TICKET_STALE_MS = 4 * 60_000

/** A short-lived URL for an image or a PDF, bound to this connection. */
export function useFileTicket(agentId: string, path: string | null, enabled: boolean) {
  const rpc = useRpc()
  return useQuery<TicketResult>({
    queryKey: [FILES_QUERY_ROOT, agentId, 'ticket', path],
    queryFn: async () => {
      await rpc.waitForConnection()
      const ticket = normalizeTicket(await rpc.call<unknown>(DOWNLOAD_METHOD, { path, agentId }))
      if (!ticket) throw new Error('The gateway returned no download URL')
      return ticket
    },
    enabled: enabled && path !== null,
    retry: false,
    staleTime: TICKET_STALE_MS,
    gcTime: TICKET_STALE_MS,
    // A reconnect is a new connection: the old ticket died with the socket.
    refetchOnReconnect: true,
  })
}
