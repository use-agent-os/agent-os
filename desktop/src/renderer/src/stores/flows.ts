import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import { useRpc } from '@/app/providers'
import type { WsRpcClient } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import {
  applySnapshot,
  emptyTracker,
  ingest,
  isTerminal,
  latestRunId,
  runsNeedingSnapshot,
  toFlowEvent,
  type RunTracker,
} from '~/views/flows/run-tracker'
import type {
  ChangedEvent,
  EstimateResult,
  FlowDoc,
  FlowEvent,
  FlowList,
  FlowSummary,
  FlowTemplate,
  ImageRef,
  MediaResult,
  ModelsPayload,
  NodeErrors,
  NodesPayload,
  RunRecord,
  RunStatus,
  RunSummary,
  ValidateResult,
} from '~/views/flows/types'

/**
 * Image Flows over the gateway's `flows.*` RPC (contract §5) and events (§6).
 *
 * Reads are react-query hooks with named keys, so an event can knock the
 * right one stale: `flows.changed` after a save/create/delete, `flows.run`
 * as a run moves, `_hello` after a reconnect. Live run progress does not go
 * through the query cache: `useFlowRunTracker` folds the events, in seq
 * order, into a per-run view and re-reads `flows.run.get` whenever it cannot
 * trust what it has.
 */

export const FLOWS_KEYS = {
  all: ['flows'] as const,
  nodes: ['flows', 'nodes'] as const,
  models: ['flows', 'models'] as const,
  list: ['flows', 'list'] as const,
  flow: (id: string) => ['flows', 'flow', id] as const,
  runs: (flowId?: string) => ['flows', 'runs', flowId ?? 'all'] as const,
  run: (runId: string) => ['flows', 'run', runId] as const,
  media: (sha: string, maxSide?: number) => ['flows', 'media', sha, maxSide ?? 0] as const,
}

/** Thumbnails on the canvas, the home grid and the tray. */
export const THUMB_SIDE = 256

/** Typed wrappers, one per method. Literal names: required-methods.test reads them. */
export function flowsApi(rpc: WsRpcClient) {
  return {
    nodes: () => rpc.call<NodesPayload>('flows.nodes', {}),
    models: () => rpc.call<ModelsPayload>('flows.models', {}),
    list: () => rpc.call<WriteAccess & { flows: FlowSummary[] }>('flows.list', {}),
    create: (name: string, template?: FlowTemplate) =>
      rpc.call<{ flow: FlowDoc }>('flows.create', template ? { name, template } : { name }),
    get: (id: string) => rpc.call<WriteAccess & { flow: FlowDoc }>('flows.get', { id }),
    save: (flow: FlowDoc, baseRev: number) =>
      rpc.call<{ flow: FlowDoc }>('flows.save', { flow, base_rev: baseRev }),
    remove: (id: string) => rpc.call<{ ok: boolean }>('flows.delete', { id }),
    validate: (flow: FlowDoc, toNode?: string) =>
      rpc.call<ValidateResult>('flows.validate', toNode ? { flow, to_node: toNode } : { flow }),
    estimate: (flow: FlowDoc, toNode?: string, inputs?: Record<string, unknown>) =>
      rpc.call<EstimateResult>('flows.estimate', {
        flow,
        ...(toNode ? { to_node: toNode } : {}),
        ...(inputs ? { inputs } : {}),
      }),
    run: (params: {
      flow_id: string
      flow?: FlowDoc
      inputs?: Record<string, unknown>
      to_node?: string
    }) => rpc.call<{ run_id: string; status: RunStatus }>('flows.run', params),
    stop: (runId: string) => rpc.call<{ ok: boolean }>('flows.stop', { run_id: runId }),
    runs: (flowId?: string, limit?: number) =>
      rpc.call<{ runs: RunSummary[] }>('flows.runs', {
        ...(flowId ? { flow_id: flowId } : {}),
        ...(limit ? { limit } : {}),
      }),
    runGet: (runId: string) => rpc.call<{ run: RunRecord }>('flows.run.get', { run_id: runId }),
    media: (sha256: string, maxSide?: number) =>
      rpc.call<MediaResult>('flows.media', maxSide ? { sha256, max_side: maxSide } : { sha256 }),
    upload: (name: string, mime: string, dataB64: string) =>
      rpc.call<{ image: ImageRef }>('flows.upload', { name, mime, data_b64: dataB64 }),
    importPng: (dataB64: string) =>
      rpc.call<{ image: ImageRef; flow: FlowDoc | null }>('flows.import_png', {
        data_b64: dataB64,
      }),
  }
}

export type FlowsApi = ReturnType<typeof flowsApi>

/** Whether this connection may write flows (`flows.get` / `flows.list`). */
export interface WriteAccess {
  writable?: boolean
  read_only_reason?: string | null
}

/**
 * The write access a `flows.get` answer carries, on the result or on the
 * document. Absent means writable (an older gateway).
 */
export function docWithAccess(res: WriteAccess & { flow: FlowDoc }): FlowDoc {
  const writable = res.writable ?? res.flow.writable
  const reason = res.read_only_reason ?? res.flow.read_only_reason ?? null
  return { ...res.flow, writable: writable !== false, read_only_reason: reason }
}

/** The same for `flows.list`: on the result, or on any listed flow. */
export function listWithAccess(res: WriteAccess & { flows?: FlowSummary[] }): FlowList {
  const flows = Array.isArray(res?.flows) ? res.flows : []
  const refused = flows.find((f) => f.writable === false)
  const writable = res?.writable ?? (refused ? false : true)
  const readOnlyReason = res?.read_only_reason ?? refused?.read_only_reason ?? null
  return { flows, writable: writable !== false, readOnlyReason }
}

export function useFlowsApi(): FlowsApi {
  const rpc = useRpc()
  return useMemo(() => flowsApi(rpc), [rpc])
}

// ── Errors ──────────────────────────────────────────────────────────────────

export const FLOW_ERRORS = {
  notFound: 'flows.not_found',
  conflict: 'flows.conflict',
  invalid: 'flows.invalid',
  busy: 'flows.busy',
  unavailable: 'flows.unavailable',
  costGate: 'flows.cost_gate',
  /** The gateway treats this connection as an agent's: no edits, no runs. */
  operatorRequired: 'flows.operator_required',
} as const

function detailsOf(err: unknown): Record<string, unknown> | null {
  if (!err || typeof err !== 'object' || !('details' in err)) return null
  const d = (err as { details?: unknown }).details
  return d && typeof d === 'object' ? (d as Record<string, unknown>) : null
}

/**
 * The `flows.*` code of a failure. The contract puts it in `error.code`; a
 * gateway that sends a generic top-level code with the specific one in
 * `details.code` (the protocol's newer convention) is read the same.
 */
export function flowsErrorCode(err: unknown): string | null {
  if (err && typeof err === 'object' && 'code' in err) {
    const code = (err as { code?: unknown }).code
    if (typeof code === 'string' && code.startsWith('flows.')) return code
  }
  const nested = detailsOf(err)?.code
  return typeof nested === 'string' && nested.startsWith('flows.') ? nested : null
}

/** `details.node_errors` of a `flows.invalid` failure. */
export function nodeErrorsOf(err: unknown): NodeErrors | null {
  const errors = detailsOf(err)?.node_errors
  return errors && typeof errors === 'object' ? (errors as NodeErrors) : null
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

// ── Queries ─────────────────────────────────────────────────────────────────

function useConnected(): boolean {
  return useConnection((s) => s.state === 'connected')
}

export function useFlowNodes() {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<NodesPayload>({
    queryKey: FLOWS_KEYS.nodes,
    enabled: connected,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      await rpc.waitForConnection()
      return flowsApi(rpc).nodes()
    },
  })
}

/** Availability follows provider keys, which change from Settings: refetch on focus. */
export function useFlowModels() {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<ModelsPayload>({
    queryKey: FLOWS_KEYS.models,
    enabled: connected,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    queryFn: async () => {
      await rpc.waitForConnection()
      return flowsApi(rpc).models()
    },
  })
}

export function useFlowList() {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<FlowList>({
    queryKey: FLOWS_KEYS.list,
    enabled: connected,
    refetchOnWindowFocus: true,
    queryFn: async () => {
      await rpc.waitForConnection()
      return listWithAccess(await flowsApi(rpc).list())
    },
  })
}

/** One flow document, read once per open; the editor owns it after that. */
export function useFlowDoc(id: string) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<FlowDoc>({
    queryKey: FLOWS_KEYS.flow(id),
    enabled: connected && Boolean(id),
    staleTime: Infinity,
    // Read fresh on every open; the editor owns the copy while it is open.
    gcTime: 0,
    refetchOnWindowFocus: false,
    retry: (count, err) => flowsErrorCode(err) !== FLOW_ERRORS.notFound && count < 2,
    queryFn: async () => {
      await rpc.waitForConnection()
      return docWithAccess(await flowsApi(rpc).get(id))
    },
  })
}

export function useFlowRuns(flowId: string, limit = 30) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<RunSummary[]>({
    queryKey: FLOWS_KEYS.runs(flowId),
    enabled: connected && Boolean(flowId),
    queryFn: async () => {
      await rpc.waitForConnection()
      const res = await flowsApi(rpc).runs(flowId, limit)
      return Array.isArray(res?.runs) ? res.runs : []
    },
  })
}

/** A full run record, for opening a past run's images. */
export function useFlowRunRecord(runId: string | null) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<RunRecord>({
    queryKey: FLOWS_KEYS.run(runId ?? ''),
    enabled: connected && Boolean(runId),
    // A settled run never changes again.
    staleTime: (q) => (isTerminal(q.state.data?.status) ? Infinity : 0),
    queryFn: async () => {
      await rpc.waitForConnection()
      return (await flowsApi(rpc).runGet(runId!)).run
    },
  })
}

// ── Media ───────────────────────────────────────────────────────────────────

export function mediaSrc(m: MediaResult | undefined): string | null {
  if (!m?.data_b64) return null
  return `data:${m.mime || 'image/png'};base64,${m.data_b64}`
}

/**
 * Pixels for one blob, through `flows.media` (there is no HTTP route; see
 * contract §1). Blobs are content-addressed, so an answer never goes stale:
 * thumbnails (`maxSide`) and the full image are cached apart, for good.
 */
export function useFlowMedia(sha256: string | null | undefined, maxSide?: number) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<MediaResult>({
    queryKey: FLOWS_KEYS.media(sha256 ?? '', maxSide),
    enabled: connected && Boolean(sha256),
    staleTime: Infinity,
    gcTime: 15 * 60_000,
    retry: 1,
    refetchOnWindowFocus: false,
    queryFn: async () => {
      await rpc.waitForConnection()
      return flowsApi(rpc).media(sha256!, maxSide)
    },
  })
  return { ...query, src: mediaSrc(query.data) }
}

export function useFlowThumb(image: ImageRef | null | undefined) {
  return useFlowMedia(image?.sha256, THUMB_SIDE)
}

// ── Events ──────────────────────────────────────────────────────────────────

export function invalidateFlows(queryClient: QueryClient, flowId?: string): void {
  void queryClient.invalidateQueries({ queryKey: FLOWS_KEYS.list })
  if (flowId) void queryClient.invalidateQueries({ queryKey: FLOWS_KEYS.runs(flowId) })
}

/**
 * Keep the list and run history fresh while a flows screen is mounted:
 * `flows.changed` refetches the list, a run settling refetches that flow's
 * history, a reconnect refetches everything flows (debounced into one round).
 */
export function useFlowsInvalidation(): void {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  useEffect(() => {
    const pending = new Set<string>()
    let all = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const flush = () => {
      timer = null
      if (all) {
        void queryClient.invalidateQueries({
          queryKey: FLOWS_KEYS.all,
          predicate: (q) => q.queryKey[1] !== 'media' && q.queryKey[1] !== 'flow',
        })
      } else {
        void queryClient.invalidateQueries({ queryKey: FLOWS_KEYS.list })
        for (const id of pending) {
          void queryClient.invalidateQueries({ queryKey: FLOWS_KEYS.runs(id) })
        }
      }
      pending.clear()
      all = false
    }
    const schedule = () => {
      if (!timer) timer = setTimeout(flush, 200)
    }
    const offs = [
      rpc.on('flows.changed', () => schedule()),
      rpc.on('flows.run', (payload) => {
        const p = payload as { flow_id?: string; status?: RunStatus } | undefined
        if (!p?.flow_id) return
        if (p.status === 'queued' || isTerminal(p.status)) {
          pending.add(p.flow_id)
          schedule()
        }
      }),
      rpc.on('_hello', () => {
        all = true
        schedule()
      }),
    ]
    return () => {
      offs.forEach((off) => off())
      if (timer) clearTimeout(timer)
    }
  }, [rpc, queryClient])
}

/** `flows.changed` for one flow, to the editor (reload / tell the user). */
export function useFlowChanged(flowId: string, onChanged: (ev: ChangedEvent) => void): void {
  const rpc = useRpc()
  const ref = useRef(onChanged)
  useEffect(() => {
    ref.current = onChanged
  }, [onChanged])
  useEffect(() => {
    return rpc.on('flows.changed', (payload) => {
      const ev = payload as ChangedEvent | undefined
      if (ev && ev.flow_id === flowId) ref.current(ev)
    })
  }, [rpc, flowId])
}

type TrackerAction =
  | { type: 'event'; event: FlowEvent; now: number }
  | { type: 'snapshot'; record: RunRecord }
  | { type: 'reset' }

function trackerReducer(state: RunTracker, action: TrackerAction): RunTracker {
  switch (action.type) {
    case 'event':
      return ingest(state, action.event, action.now)
    case 'snapshot':
      return applySnapshot(state, action.record)
    case 'reset':
      return emptyTracker()
  }
}

/**
 * Live state of this flow's runs. Folds `flows.run` / `flows.node` (only this
 * flow's) in seq order, takes snapshots from `flows.run.get` for runs it
 * joined mid-way or lost a frame of, and on reconnect re-reads every run it
 * knows that has not settled. `seed` loads one run up front (the newest), so
 * opening a flow shows what its last run produced.
 */
export function useFlowRunTracker(flowId: string, seedRunId: string | null) {
  const rpc = useRpc()
  const [tracker, dispatch] = useReducer(trackerReducer, undefined, emptyTracker)
  const trackerRef = useRef(tracker)
  useEffect(() => {
    trackerRef.current = tracker
  }, [tracker])
  const inFlight = useRef(new Set<string>())

  const snapshot = useCallback(
    async (runId: string) => {
      if (inFlight.current.has(runId)) return
      inFlight.current.add(runId)
      try {
        await rpc.waitForConnection()
        const res = await flowsApi(rpc).runGet(runId)
        if (res?.run) dispatch({ type: 'snapshot', record: res.run })
      } catch {
        // Next gap check or reconnect asks again.
      } finally {
        inFlight.current.delete(runId)
      }
    },
    [rpc],
  )

  useEffect(() => {
    const onEvent = (kind: 'run' | 'node') => (payload: unknown) => {
      const event = toFlowEvent(kind, payload)
      if (!event || event.flow_id !== flowId) return
      dispatch({ type: 'event', event, now: Date.now() })
      // A settled run (and its corrected cost after a Stop): read the record
      // for the gateway's own list of what it produced.
      if (event.kind === 'run' && isTerminal(event.status)) {
        setTimeout(() => void snapshot(event.run_id), 250)
      }
    }
    const offs = [
      rpc.on('flows.run', onEvent('run')),
      rpc.on('flows.node', onEvent('node')),
      rpc.on('_hello', () => {
        for (const [runId, slot] of Object.entries(trackerRef.current.runs)) {
          if (!isTerminal(slot.view.status)) void snapshot(runId)
        }
      }),
    ]
    return () => offs.forEach((off) => off())
  }, [rpc, flowId, snapshot])

  // Gaps and mid-run joins: checked on a short beat while any exist.
  const needs = runsNeedingSnapshot(tracker, Number.MAX_SAFE_INTEGER, 0)
  const needsKey = needs.join(',')
  useEffect(() => {
    if (!needsKey) return
    const check = () => {
      for (const runId of runsNeedingSnapshot(trackerRef.current, Date.now())) {
        void snapshot(runId)
      }
    }
    const timer = setInterval(check, 750)
    const first = setTimeout(check, 0)
    return () => {
      clearInterval(timer)
      clearTimeout(first)
    }
  }, [needsKey, snapshot])

  useEffect(() => {
    if (seedRunId && !trackerRef.current.runs[seedRunId]) void snapshot(seedRunId)
  }, [seedRunId, snapshot])

  return {
    tracker,
    latestRunId: latestRunId(tracker, flowId),
    snapshot,
  }
}
