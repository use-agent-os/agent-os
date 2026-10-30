/**
 * Image Flows wire types: the gateway's `flows.*` RPC and events, exactly as
 * the implementation contract (v1) fixes them. Field names stay snake_case so
 * a payload is the object the gateway sent, not a copy renamed on the way in.
 */

/** Link types. Compatibility is exact string equality (contract §1). */
export type LinkType = 'TEXT' | 'IMAGE' | (string & {})

/** Content-addressed image blob. Pixels only through `flows.media`. */
export interface ImageRef {
  sha256: string
  mime: string
  width: number
  height: number
  bytes: number
}

// ── Flow document (§2) ──────────────────────────────────────────────────────

export interface FlowNodeDoc {
  id: string
  type: string
  pos: [number, number]
  title?: string | null
  /** Keyed by input name; a missing key means "use the schema default". */
  values: Record<string, unknown>
}

export interface FlowLinkDoc {
  id: string
  from: [string, string]
  to: [string, string]
}

export interface Viewport {
  x: number
  y: number
  zoom: number
}

export interface FlowDoc {
  version: 1
  id: string
  name: string
  rev: number
  updated?: string
  nodes: FlowNodeDoc[]
  links: FlowLinkDoc[]
  /** Public name → `"<node_id>.<input_name>"`. */
  inputs: Record<string, string>
  viewport?: Viewport
  /**
   * Not stored: `flows.get` says whether this connection may write (it may
   * put it on the result or on the document; the store copies it here).
   */
  writable?: boolean
  read_only_reason?: string | null
}

// ── Node schema (§3) ────────────────────────────────────────────────────────

export type WidgetKind =
  'text' | 'textarea' | 'number' | 'combo' | 'toggle' | 'model' | 'variation' | 'image_upload'

export type OptionsFrom = 'model.ratios' | 'model.tiers' | 'model.sizes' | 'model.qualities'

export type CapKey = 'ratios' | 'tiers' | 'sizes' | 'qualities' | 'transparent_bg'

export interface InputSpec {
  name: string
  type?: LinkType
  widget?: WidgetKind
  socket?: boolean
  optional?: boolean
  multi?: boolean
  default?: unknown
  min?: number
  max?: number
  step?: number
  options?: string[]
  options_from?: OptionsFrom
  show_when?: { 'model.has'?: CapKey }
  placeholder?: string
  tooltip?: string
  advanced?: boolean
}

export interface OutputSpec {
  name: string
  type: LinkType
}

export type NodeGroup = 'input' | 'utility' | 'ai' | 'output'

export interface NodeSchema {
  id: string
  display: string
  group: NodeGroup
  description?: string
  is_output?: boolean
  is_api?: boolean
  inputs: InputSpec[]
  outputs: OutputSpec[]
  price?: { depends_on: string[] } | null
}

export interface NodesPayload {
  version: number
  types: Record<string, { color: string }>
  nodes: NodeSchema[]
}

// ── Models (§4) ─────────────────────────────────────────────────────────────

export interface ModelOutputCaps {
  ratios?: string[]
  tiers?: string[]
  sizes?: string[]
  qualities?: string[]
  transparent_bg?: boolean
  n_max?: number
}

export interface ModelPrice {
  per_image?: Record<string, number>
  per_ref?: number
  approximate?: boolean
  source?: string
}

export interface ModelWire {
  id: string
  display: string
  provider: string
  family?: string
  status?: 'active' | 'deprecated' | (string & {})
  available: boolean
  unavailable_reason?: string | null
  ops: { t2i?: boolean; edit?: boolean; mask?: boolean }
  max_images?: number
  output: ModelOutputCaps
  seed_honored?: boolean
  price?: ModelPrice | null
}

export interface ModelsPayload {
  default: string | null
  models: ModelWire[]
}

// ── RPC results (§5) ────────────────────────────────────────────────────────

export interface FlowSummary {
  id: string
  name: string
  rev: number
  updated: string
  node_count: number
  preview: ImageRef | null
  writable?: boolean
  read_only_reason?: string | null
}

/** `flows.list`, with whether this connection may create, edit or delete flows. */
export interface FlowList {
  flows: FlowSummary[]
  writable: boolean
  readOnlyReason: string | null
}

export type FlowTemplate = 'blank' | 'text_to_image' | 'edit_image' | 'describe_remix'

export interface NodeError {
  input?: string
  code: string
  message: string
}

export type NodeErrors = Record<string, NodeError[]>

export interface ValidateResult {
  ok: boolean
  node_errors: NodeErrors
}

export interface Price {
  kind: 'free' | 'usd' | 'range' | 'unknown'
  usd?: number
  min?: number
  max?: number
  approximate: boolean
}

export interface EstimateResult {
  nodes: Record<string, Price>
  total: Price
  will_run: string[]
  cached: string[]
}

export type RunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'

export type NodeStateKind = 'queued' | 'running' | 'cached' | 'done' | 'error' | 'skipped'

export interface NodeOutputs {
  images?: ImageRef[]
  text?: string
  /** A provider remark worth showing (it returned another size than asked). */
  note?: string
}

export interface NodeState {
  state: NodeStateKind
  ms?: number
  cost_usd?: number
  progress?: { value: number; max: number }
  error?: NodeError
}

export interface RunSummary {
  run_id: string
  flow_id: string
  rev: number
  status: RunStatus
  created: string
  started?: string | null
  ended?: string | null
  est_usd?: number | null
  cost_usd?: number | null
  preview: ImageRef | null
  origin: 'canvas' | 'agent' | 'cli'
  error: { code?: string; message?: string; node_id?: string } | null
  image_count: number
}

export interface RunRecord extends RunSummary {
  inputs?: Record<string, unknown>
  graph?: FlowDoc
  nodes: Record<string, NodeState>
  outputs: Record<string, NodeOutputs>
  /** New images this run produced: Save outputs first, then image nodes that ran; unique. */
  images?: ImageRef[]
  /**
   * Not in contract v1. When the engine stamps the last event seq on the
   * snapshot the tracker uses it; otherwise it falls back to the highest seq
   * it has seen.
   */
  seq?: number
}

export interface MediaResult {
  mime: string
  data_b64: string
  width: number
  height: number
}

// ── Events (§6) ─────────────────────────────────────────────────────────────

export interface RunEvent {
  run_id: string
  flow_id: string
  seq: number
  status: RunStatus
  est_usd?: number
  cost_usd?: number
  error?: { code?: string; message?: string; node_id?: string } | null
}

export interface NodeEvent {
  run_id: string
  flow_id: string
  seq: number
  node_id: string
  state: NodeStateKind
  progress?: { value: number; max: number }
  ms?: number
  cost_usd?: number
  outputs?: NodeOutputs
  error?: NodeError
}

export interface ChangedEvent {
  flow_id: string
  rev: number
  deleted?: boolean
}

export type FlowEvent = ({ kind: 'run' } & RunEvent) | ({ kind: 'node' } & NodeEvent)
