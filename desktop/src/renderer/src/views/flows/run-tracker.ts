import type {
  FlowDoc,
  NodeSchema,
  FlowEvent,
  ImageRef,
  NodeEvent,
  NodeOutputs,
  NodeState,
  NodeStateKind,
  RunEvent,
  RunRecord,
  RunStatus,
} from './types'

/**
 * Live run state from `flows.run` / `flows.node` events (contract §6).
 *
 * Every event carries a per-run `seq`. Events are applied strictly in seq
 * order: a duplicate or older one is dropped, one that skips ahead waits in a
 * buffer until the gap fills. A gap that does not fill (a lost frame, a
 * reconnect) or a run joined mid-way asks for a `flows.run.get` snapshot,
 * which replaces the view; buffered events are replayed over it.
 *
 * Neither a run nor a node ever moves backwards (queued → running → settled),
 * so a stale event replayed over a newer snapshot cannot undo it.
 */

/**
 * A node as the canvas shows it. On top of the wire states there is
 * `stopping`: the run was stopped while this node's provider call was in
 * flight; the engine lets that call finish (it is paid for) and reports it
 * later. `stopped` marks a node whose result arrived after the Stop.
 */
export type ViewNodeKind = NodeStateKind | 'stopping'

export interface ViewNodeState extends Omit<NodeState, 'state'> {
  state: ViewNodeKind
  stopped?: boolean
}

export interface RunView {
  run_id: string
  flow_id: string
  status: RunStatus
  est_usd?: number | null
  cost_usd?: number | null
  error?: { code?: string; message?: string; node_id?: string } | null
  nodes: Record<string, ViewNodeState>
  outputs: Record<string, NodeOutputs>
  created?: string
  graph?: FlowDoc
  /** The run's new images as the gateway lists them (from `flows.run.get`), when known. */
  images?: ImageRef[]
  /** Local clock (ms) when the run was first seen running, for the elapsed time. */
  startedAt?: number
  /** Local clock (ms) when each node was first seen running. */
  nodeStartedAt: Record<string, number>
}

interface RunSlot {
  view: RunView
  /** Highest seq applied, in order. */
  applied: number
  /** Events past a gap, waiting for the missing ones. */
  buffer: FlowEvent[]
  /** When the current gap opened (ms), or null. */
  gapSince: number | null
  /** Highest seq ever received. */
  maxSeen: number
  /** Joined mid-run: the start was never seen. */
  needsSnapshot: boolean
  /** Order of first sight, for "latest run". */
  seenAt: number
}

export interface RunTracker {
  runs: Record<string, RunSlot>
  clock: number
}

export function emptyTracker(): RunTracker {
  return { runs: {}, clock: 0 }
}

const RUN_RANK: Record<RunStatus, number> = {
  queued: 0,
  running: 1,
  done: 2,
  failed: 2,
  cancelled: 2,
}

const NODE_RANK: Record<ViewNodeKind, number> = {
  queued: 0,
  running: 1,
  stopping: 2,
  cached: 2,
  done: 2,
  error: 2,
  skipped: 2,
}

export function isTerminal(status: RunStatus | undefined): boolean {
  return status === 'done' || status === 'failed' || status === 'cancelled'
}

export function isSettledNode(state: ViewNodeKind | undefined): boolean {
  return state !== undefined && NODE_RANK[state] === 2
}

function blankView(ev: { run_id: string; flow_id: string }): RunView {
  return {
    run_id: ev.run_id,
    flow_id: ev.flow_id,
    status: 'queued',
    nodes: {},
    outputs: {},
    nodeStartedAt: {},
  }
}

/**
 * A run that is over has no node still working. Whatever had not started is
 * `skipped`. A node whose call was in flight when the run was cancelled is
 * `stopping` (the engine lets the call finish and reports it later); after
 * an error or a shutdown it is `skipped`. Applied whether or not the gateway
 * sent node events saying so.
 */
export function settleNodes(view: RunView): RunView {
  if (!isTerminal(view.status)) return view
  let nodes: Record<string, ViewNodeState> | null = null
  for (const [id, node] of Object.entries(view.nodes)) {
    if (isSettledNode(node.state)) continue
    nodes ??= { ...view.nodes }
    const finishing = node.state === 'running' && view.status === 'cancelled'
    nodes[id] = {
      state: finishing ? 'stopping' : 'skipped',
      ...(node.ms !== undefined ? { ms: node.ms } : {}),
      ...(node.cost_usd !== undefined ? { cost_usd: node.cost_usd } : {}),
    }
  }
  return nodes ? { ...view, nodes } : view
}

function applyRun(view: RunView, ev: RunEvent, now: number): RunView {
  if (RUN_RANK[ev.status] < RUN_RANK[view.status]) return view
  // A settled run keeps its status; a late event may still correct its cost.
  const status = isTerminal(view.status) ? view.status : ev.status
  return settleNodes({
    ...view,
    status,
    est_usd: ev.est_usd ?? view.est_usd,
    cost_usd: ev.cost_usd ?? view.cost_usd,
    error: isTerminal(view.status) ? view.error : (ev.error ?? view.error),
    startedAt: view.startedAt ?? (status === 'running' ? now : undefined),
  })
}

function applyNode(view: RunView, ev: NodeEvent, now: number): RunView {
  const prev = view.nodes[ev.node_id]
  // After the run is over nothing runs again. A call that was in flight at
  // Stop lands later: its result is kept (paid for, cached, in the tray) and
  // the node reads "stopped"; anything else only corrects what it cost.
  if (isTerminal(view.status)) {
    if (!prev) return view
    if (prev.state === 'stopping' && isSettledNode(ev.state)) {
      const landed = ev.state === 'done' || ev.state === 'cached'
      const cost = ev.cost_usd ?? prev.cost_usd
      const ms = ev.ms ?? prev.ms
      const node: ViewNodeState = {
        state: landed ? 'done' : 'skipped',
        stopped: true,
        ...(ms !== undefined ? { ms } : {}),
        ...(cost !== undefined ? { cost_usd: cost } : {}),
      }
      const outputs =
        landed && ev.outputs ? { ...view.outputs, [ev.node_id]: ev.outputs } : view.outputs
      return { ...view, nodes: { ...view.nodes, [ev.node_id]: node }, outputs }
    }
    if (ev.cost_usd === undefined) return view
    return { ...view, nodes: { ...view.nodes, [ev.node_id]: { ...prev, cost_usd: ev.cost_usd } } }
  }
  if (prev && NODE_RANK[ev.state] < NODE_RANK[prev.state]) return view
  if (prev && isSettledNode(prev.state) && isSettledNode(ev.state) && prev.state !== ev.state) {
    return view
  }
  const state: ViewNodeState = {
    state: ev.state,
    ...(ev.ms !== undefined ? { ms: ev.ms } : prev?.ms !== undefined ? { ms: prev.ms } : {}),
    ...(ev.cost_usd !== undefined
      ? { cost_usd: ev.cost_usd }
      : prev?.cost_usd !== undefined
        ? { cost_usd: prev.cost_usd }
        : {}),
    ...(ev.state === 'running' && ev.progress ? { progress: ev.progress } : {}),
    ...(ev.error ? { error: ev.error } : {}),
  }
  const nodes = { ...view.nodes, [ev.node_id]: state }
  const outputs = ev.outputs ? { ...view.outputs, [ev.node_id]: ev.outputs } : view.outputs
  const nodeStartedAt =
    ev.state === 'running' && view.nodeStartedAt[ev.node_id] === undefined
      ? { ...view.nodeStartedAt, [ev.node_id]: now }
      : view.nodeStartedAt
  // A node can only be running while its run is.
  const status = ev.state === 'running' && view.status === 'queued' ? 'running' : view.status
  const startedAt = view.startedAt ?? (status === 'running' ? now : undefined)
  return { ...view, nodes, outputs, status, nodeStartedAt, startedAt }
}

function applyEvent(view: RunView, ev: FlowEvent, now: number): RunView {
  return ev.kind === 'run' ? applyRun(view, ev, now) : applyNode(view, ev, now)
}

function drain(slot: RunSlot, now: number): RunSlot {
  let { view, applied } = slot
  const keep: FlowEvent[] = []
  // Ascending, so once one event waits on a gap every later one does too.
  for (const ev of [...slot.buffer].sort((a, b) => a.seq - b.seq)) {
    if (ev.seq <= applied) continue
    if (ev.seq === applied + 1 && keep.length === 0) {
      view = applyEvent(view, ev, now)
      applied = ev.seq
    } else {
      keep.push(ev)
    }
  }
  return {
    ...slot,
    view,
    applied,
    buffer: keep,
    gapSince: keep.length ? slot.gapSince : null,
  }
}

/** Feed one event. `now` (ms) times gaps and when nodes started running. */
export function ingest(tr: RunTracker, ev: FlowEvent, now = Date.now()): RunTracker {
  if (!ev || typeof ev.seq !== 'number' || !ev.run_id) return tr
  const clock = tr.clock + 1
  let slot = tr.runs[ev.run_id]
  if (!slot) {
    slot = {
      view: blankView(ev),
      applied: ev.seq - 1,
      buffer: [],
      gapSince: null,
      maxSeen: 0,
      needsSnapshot: ev.seq > 1,
      seenAt: clock,
    }
  }
  if (ev.seq <= slot.applied || slot.buffer.some((b) => b.seq === ev.seq)) {
    return tr
  }
  const next = drain(
    {
      ...slot,
      buffer: [...slot.buffer, ev],
      maxSeen: Math.max(slot.maxSeen, ev.seq),
      gapSince: slot.gapSince ?? (ev.seq > slot.applied + 1 ? now : null),
    },
    now,
  )
  return { runs: { ...tr.runs, [ev.run_id]: next }, clock }
}

/** Replace a run's view with a `flows.run.get` snapshot, then replay what is buffered. */
export function applySnapshot(tr: RunTracker, record: RunRecord, now = Date.now()): RunTracker {
  const clock = tr.clock + 1
  const prev = tr.runs[record.run_id]
  const nodes = { ...(record.nodes ?? {}) }
  // Keep the start times already seen; a node first seen running here starts now.
  const nodeStartedAt: Record<string, number> = {}
  for (const [id, node] of Object.entries(nodes)) {
    if (node.state !== 'running') continue
    nodeStartedAt[id] = prev?.view.nodeStartedAt[id] ?? now
  }
  const started = record.started ? Date.parse(record.started) : NaN
  let view: RunView = {
    run_id: record.run_id,
    flow_id: record.flow_id,
    status: record.status,
    est_usd: record.est_usd,
    cost_usd: record.cost_usd,
    error: record.error,
    nodes,
    outputs: { ...(record.outputs ?? {}) },
    created: record.created,
    graph: record.graph,
    ...(Array.isArray(record.images) ? { images: record.images } : {}),
    startedAt:
      prev?.view.startedAt ??
      (Number.isFinite(started) ? started : record.status === 'running' ? now : undefined),
    nodeStartedAt,
  }
  const base = typeof record.seq === 'number' ? record.seq : (prev?.maxSeen ?? 0)
  const replay = (prev?.buffer ?? []).filter((ev) => ev.seq > base).sort((a, b) => a.seq - b.seq)
  // Without a seq on the snapshot, anything already applied may be newer than
  // it: replay those too; the no-backwards rule keeps it safe.
  for (const ev of replay) view = applyEvent(view, ev, now)
  view = keepStopped(settleNodes(view), prev?.view)
  const applied = Math.max(base, prev?.maxSeen ?? 0, ...replay.map((e) => e.seq))
  const slot: RunSlot = {
    view,
    applied,
    buffer: [],
    gapSince: null,
    maxSeen: Math.max(prev?.maxSeen ?? 0, applied),
    needsSnapshot: false,
    seenAt: prev?.seenAt ?? clock,
  }
  return { runs: { ...tr.runs, [record.run_id]: slot }, clock }
}

/**
 * A record does not say a node's result landed after Stop; the live view
 * did. Keep that (and a node still finishing) across a snapshot.
 */
function keepStopped(view: RunView, prev: RunView | undefined): RunView {
  if (!prev || view.status !== 'cancelled') return view
  let nodes: Record<string, ViewNodeState> | null = null
  for (const [id, before] of Object.entries(prev.nodes)) {
    const now = view.nodes[id]
    if (!now || !(before.state === 'stopping' || before.stopped)) continue
    if (now.state === 'stopping' || now.stopped) continue
    nodes ??= { ...view.nodes }
    nodes[id] = isSettledNode(now.state) ? { ...now, stopped: true } : before
  }
  return nodes ? { ...view, nodes } : view
}

/** A stored run record as a view (no live state), for reading its images. */
export function viewOfRecord(record: RunRecord): RunView {
  return settleNodes({
    run_id: record.run_id,
    flow_id: record.flow_id,
    status: record.status,
    cost_usd: record.cost_usd,
    est_usd: record.est_usd,
    error: record.error,
    nodes: record.nodes ?? {},
    outputs: record.outputs ?? {},
    created: record.created,
    graph: record.graph,
    ...(Array.isArray(record.images) ? { images: record.images } : {}),
    nodeStartedAt: {},
  })
}

/** Runs whose events cannot be trusted any more: joined mid-way, or a gap older than `ms`. */
export function runsNeedingSnapshot(tr: RunTracker, now = Date.now(), ms = 1500): string[] {
  return Object.entries(tr.runs)
    .filter(([, s]) => s.needsSnapshot || (s.gapSince !== null && now - s.gapSince >= ms))
    .map(([id]) => id)
}

export function runView(tr: RunTracker, runId: string | null | undefined): RunView | null {
  if (!runId) return null
  return tr.runs[runId]?.view ?? null
}

/** The run most recently seen for a flow (by first event or snapshot). */
export function latestRunId(tr: RunTracker, flowId: string): string | null {
  let best: RunSlot | null = null
  for (const slot of Object.values(tr.runs)) {
    if (slot.view.flow_id !== flowId) continue
    if (!best || slot.seenAt > best.seenAt) best = slot
  }
  return best?.view.run_id ?? null
}

/**
 * The new images a run produced, one per blob: the gateway's own list when
 * the view has it (`RunRecord.images`), else the same rule applied here:
 * what output nodes (Save) wrote first, then what other nodes that actually
 * ran produced. Images a node served from cache, and the images the run was
 * given (Load Image), are not this run's results.
 *
 * `typeOf` names a node's type when the view has no graph (live events);
 * `schemas` says which types are inputs and outputs.
 */
export function runImages(
  view: RunView | null,
  typeOf?: (nodeId: string) => string | undefined,
  schemas?: ReadonlyMap<string, NodeSchema>,
): ImageRef[] {
  if (!view) return []
  if (Array.isArray(view.images)) return view.images
  const graphType = new Map((view.graph?.nodes ?? []).map((n) => [n.id, n.type]))
  const kindOf = (nodeId: string): 'input' | 'output' | 'other' => {
    const type = graphType.get(nodeId) ?? typeOf?.(nodeId)
    const schema = type ? schemas?.get(type) : undefined
    if (type === 'load_image' || schema?.group === 'input') return 'input'
    if (type === 'save' || schema?.is_output) return 'output'
    return 'other'
  }
  // Blobs this run made, and blobs it only carried along (cache hits, inputs).
  const fresh = new Set<string>()
  const carried = new Set<string>()
  for (const [nodeId, outputs] of Object.entries(view.outputs)) {
    const kind = kindOf(nodeId)
    if (kind === 'output') continue
    const made = kind === 'other' && view.nodes[nodeId]?.state !== 'cached'
    for (const img of outputs.images ?? []) {
      if (img?.sha256) (made ? fresh : carried).add(img.sha256)
    }
  }
  const seen = new Set<string>()
  const out: ImageRef[] = []
  const take = (want: 'output' | 'other') => {
    for (const [nodeId, outputs] of Object.entries(view.outputs)) {
      if (kindOf(nodeId) !== want) continue
      if (want === 'other' && view.nodes[nodeId]?.state === 'cached') continue
      for (const img of outputs.images ?? []) {
        if (!img?.sha256 || seen.has(img.sha256)) continue
        // A Save of a cached (or loaded) image did not produce it.
        if (want === 'output' && carried.has(img.sha256) && !fresh.has(img.sha256)) continue
        seen.add(img.sha256)
        out.push(img)
      }
    }
  }
  take('output')
  take('other')
  return out
}

/** "Image · 2/4" style progress for the tray header: the node running furthest along. */
export function runProgress(view: RunView | null): { done: number; total: number } {
  if (!view) return { done: 0, total: 0 }
  const states = Object.values(view.nodes)
  const total = states.length
  const done = states.filter((s) => isSettledNode(s.state)).length
  return { done, total }
}

/** Parse a raw event payload into a typed FlowEvent (null when it is not one). */
export function toFlowEvent(kind: 'run' | 'node', payload: unknown): FlowEvent | null {
  if (!payload || typeof payload !== 'object') return null
  const p = payload as Record<string, unknown>
  if (typeof p.run_id !== 'string' || typeof p.flow_id !== 'string') return null
  if (typeof p.seq !== 'number') return null
  if (kind === 'run') {
    if (typeof p.status !== 'string') return null
    return { kind: 'run', ...(p as unknown as RunEvent) }
  }
  if (typeof p.node_id !== 'string' || typeof p.state !== 'string') return null
  return { kind: 'node', ...(p as unknown as NodeEvent) }
}
