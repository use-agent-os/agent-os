// Chat transcript — Image Flow run cards.
//
// The agent's `flow_run` tool publishes a JSON artifact with the
// `application/vnd.agentos.flow-run+json` mime (docs/flows.md): a snapshot of
// one run of one of the user's Image Flows. The artifact renderer emits a
// mount placeholder for it; this module fetches the snapshot, draws the card,
// then re-reads the run with `flows.run.get` (a snapshot is only as fresh as
// the moment it was published, and a transcript re-render re-mounts every
// card from it) and keeps it current from the `flows.run` / `flows.node`
// events while the run is still going.
//
// Read-only on every surface. Thumbnails come from `flows.media` (max_side
// 256) over the same connection — there is no HTTP route for flow media — so
// a card without a `call` shows the file names alone. "Open in Flows" exists
// only while the caller hands over `openFlow` (the desktop, which has the
// editor; the web console has none).
//
// Styling: the markup carries `flowrun-*` classes plus `data-flowrun-status`.
// The web console styles them in chat-unified.css; the desktop restyles the
// same hooks in its own chat.css.
//
// SECURITY: every payload-derived string reaches the DOM through
// `textContent` or an attribute setter, never `innerHTML` (flow names and file
// names are user text). Thumbnails are `data:` URLs built from the gateway's
// base64 and an `image/*` mime only.

import { t } from '@/i18n'
import '@/i18n/en/chat'

export { FLOW_RUN_ARTIFACT_MIME, isFlowRunArtifact } from './flowRunMime'

/** Longest side of a card thumbnail (px), as asked of `flows.media`. */
export const FLOW_RUN_THUMB_SIDE = 256

/** Thumbnails drawn per card; the rest is "+N more". */
export const FLOW_RUN_MAX_THUMBS = 8

/** Events for a running card are folded into one re-read per this many ms. */
export const FLOW_RUN_REFRESH_MS = 400

/** Thumbnails remembered across re-mounts (they are small and immutable). */
const THUMB_CACHE_MAX = 64

/* ── Payload shape (docs/flows.md) ──────────────────────────────────────── */

export type FlowRunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'unknown'

export interface FlowRunImage {
  sha256: string
  mime: string
  width: number | null
  height: number | null
  /** The saved file's name, or null for an image no Save node wrote. */
  name: string | null
}

export interface FlowRunCard {
  runId: string
  flowId: string
  flowName: string
  status: FlowRunStatus
  progress: { done: number; total: number }
  cached: number
  estUsd: number | null
  costUsd: number | null
  images: FlowRunImage[]
  error: { code: string; message: string } | null
}

export type FlowRunCall = (method: string, params: Record<string, unknown>) => Promise<unknown>
export type FlowRunOpen = (flowId: string) => void

const STATUSES: readonly FlowRunStatus[] = ['queued', 'running', 'done', 'failed', 'cancelled']
const TERMINAL: ReadonlySet<FlowRunStatus> = new Set(['done', 'failed', 'cancelled'])
const SHA256_RE = /^[0-9a-f]{64}$/

type Obj = Record<string, unknown>

function obj(value: unknown): Obj | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Obj) : null
}

function text(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

function num(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function count(value: unknown): number {
  const n = num(value)
  return n !== null && n > 0 ? Math.floor(n) : 0
}

function status(value: unknown): FlowRunStatus {
  const raw = text(value).toLowerCase()
  return (STATUSES as readonly string[]).includes(raw) ? (raw as FlowRunStatus) : 'unknown'
}

function errorOf(value: unknown): FlowRunCard['error'] {
  const row = obj(value)
  if (!row) return null
  const code = text(row.code)
  const message = text(row.message)
  return code || message ? { code, message: message || code } : null
}

function image(value: unknown): FlowRunImage | null {
  const row = obj(value)
  const sha = text(row?.sha256).toLowerCase()
  if (!row || !SHA256_RE.test(sha)) return null
  return {
    sha256: sha,
    mime: text(row.mime) || 'image/png',
    width: num(row.width),
    height: num(row.height),
    name: text(row.name) || null,
  }
}

function baseName(path: unknown): string | null {
  const raw = text(path)
  if (!raw) return null
  const parts = raw.split(/[\\/]/)
  return parts[parts.length - 1] || null
}

/** The card the artifact snapshot describes, or null when it is not one. */
export function normalizeFlowRunPayload(raw: unknown): FlowRunCard | null {
  const row = obj(raw)
  if (!row || row.kind !== 'flow_run') return null
  const runId = text(row.run_id)
  if (!runId) return null
  const progress = obj(row.progress)
  const images = Array.isArray(row.images)
    ? row.images.map(image).filter((i): i is FlowRunImage => i !== null)
    : []
  return {
    runId,
    flowId: text(row.flow_id),
    flowName: text(row.flow_name) || text(row.flow_id),
    status: status(row.status),
    progress: { done: count(progress?.done), total: count(progress?.total) },
    cached: count(row.cached),
    estUsd: num(row.est_usd),
    costUsd: num(row.cost_usd),
    images,
    error: errorOf(row.error),
  }
}

/**
 * The card a `flows.run.get` RunRecord describes (the same derivation as the
 * tool's `card_payload`): saved images with their file names, else the
 * generated ones; progress counts `done` and `cached` nodes.
 */
export function cardFromRun(raw: unknown, fallbackName = ''): FlowRunCard | null {
  const run = obj(raw)
  const runId = text(run?.run_id)
  if (!run || !runId) return null
  const graph = obj(run.graph)
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes.map(obj) : []
  const outputs = obj(run.outputs) ?? {}
  const saved: FlowRunImage[] = []
  const generated: FlowRunImage[] = []
  for (const node of nodes) {
    const id = text(node?.id)
    const type = text(node?.type)
    const out = obj(outputs[id])
    if (!out) continue
    const refs = Array.isArray(out.images) ? out.images : []
    const files = Array.isArray(out.files) ? out.files : []
    refs.forEach((ref, index) => {
      const parsed = image(ref)
      if (!parsed) return
      if (type === 'save') saved.push({ ...parsed, name: baseName(files[index]) })
      else if (type === 'image') generated.push({ ...parsed, name: null })
    })
  }
  const states = Object.values(obj(run.nodes) ?? {}).map((state) => text(obj(state)?.state))
  return {
    runId,
    flowId: text(run.flow_id),
    flowName: fallbackName || text(run.flow_id),
    status: status(run.status),
    progress: {
      done: states.filter((s) => s === 'done' || s === 'cached').length,
      total: states.length,
    },
    cached: states.filter((s) => s === 'cached').length,
    estUsd: num(run.est_usd),
    costUsd: num(run.cost_usd),
    images: saved.length > 0 ? saved : generated,
    error: errorOf(run.error),
  }
}

/** `$0.05`, `$0.004`; `—` when unknown. */
export function formatFlowUsd(value: number | null): string {
  if (value === null) return '—'
  return value >= 0.1 || value === 0 ? `$${value.toFixed(2)}` : `$${value.toFixed(3)}`
}

export function flowRunStatusLabel(value: FlowRunStatus): string {
  switch (value) {
    case 'queued':
      return t('chat.flowRunStatusQueued')
    case 'running':
      return t('chat.flowRunStatusRunning')
    case 'done':
      return t('chat.flowRunStatusDone')
    case 'failed':
      return t('chat.flowRunStatusFailed')
    case 'cancelled':
      return t('chat.flowRunStatusCancelled')
    default:
      return t('chat.flowRunStatusUnknown')
  }
}

export function isTerminalFlowRun(card: FlowRunCard): boolean {
  return TERMINAL.has(card.status)
}

/* ── Rendering ──────────────────────────────────────────────────────────── */

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  content?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  node.className = className
  if (content !== undefined) node.textContent = content
  return node
}

/** `data:` URL for a thumbnail, or '' when the answer is not an image. */
export function thumbnailUrl(raw: unknown): string {
  const row = obj(raw)
  const mime = text(row?.mime).toLowerCase()
  const data = text(row?.data_b64)
  if (!/^image\/[a-z0-9.+-]+$/.test(mime) || !/^[A-Za-z0-9+/=]+$/.test(data)) return ''
  return `data:${mime};base64,${data}`
}

export interface FlowRunRenderOptions {
  /** Thumbnail `data:` URL by sha256, when known. */
  thumb: (sha256: string) => string
  /** Present → the "Open in Flows" button. */
  openFlow: FlowRunOpen | null
}

/** Build the card into `host`'s body (replacing what was there). */
export function renderFlowRun(
  host: HTMLElement,
  card: FlowRunCard,
  options: FlowRunRenderOptions,
): HTMLElement {
  const body = host.querySelector<HTMLElement>('.msg-artifact-flowrun__body') ?? host
  const root = el('div', 'flowrun-card')
  root.setAttribute('data-flowrun-status', card.status)
  root.setAttribute('data-flowrun-id', card.runId)

  const head = el('div', 'flowrun-card__head')
  const eyebrow = el('span', 'flowrun-card__eyebrow', t('chat.flowRunEyebrow'))
  const title = el('span', 'flowrun-card__title', card.flowName || card.flowId)
  title.title = card.flowId
  const badge = el('span', 'flowrun-card__status', flowRunStatusLabel(card.status))
  head.append(eyebrow, title, badge)
  root.append(head)

  const meta = el('div', 'flowrun-card__meta')
  const progress =
    card.progress.total > 0
      ? t('chat.flowRunProgress', {
          done: String(card.progress.done),
          total: String(card.progress.total),
        })
      : ''
  const parts: string[] = []
  if (progress) parts.push(progress)
  if (card.cached > 0) parts.push(t('chat.flowRunCached', { count: String(card.cached) }))
  if (card.costUsd !== null)
    parts.push(t('chat.flowRunCost', { cost: formatFlowUsd(card.costUsd) }))
  else if (card.estUsd !== null)
    parts.push(t('chat.flowRunEstimate', { cost: formatFlowUsd(card.estUsd) }))
  parts.forEach((part, index) => {
    const span = el('span', 'flowrun-card__fact', part)
    if (index === 0 && progress) span.classList.add('flowrun-card__progress')
    meta.append(span)
  })
  if (card.progress.total > 0) {
    const bar = el('span', 'flowrun-card__bar')
    const fill = el('span', 'flowrun-card__bar-fill')
    fill.style.width = `${Math.round((card.progress.done / card.progress.total) * 100)}%`
    bar.append(fill)
    meta.append(bar)
  }
  root.append(meta)

  if (card.images.length > 0) {
    const grid = el('div', 'flowrun-card__thumbs')
    card.images.slice(0, FLOW_RUN_MAX_THUMBS).forEach((img, index) => {
      const figure = el('figure', 'flowrun-thumb')
      figure.setAttribute('data-flowrun-sha', img.sha256)
      const label = img.name || t('chat.flowRunImage', { n: String(index + 1) })
      const src = options.thumb(img.sha256)
      if (src) {
        const picture = document.createElement('img')
        picture.className = 'flowrun-thumb__img'
        picture.alt = label
        picture.loading = 'lazy'
        picture.src = src
        figure.append(picture)
      } else {
        figure.append(el('span', 'flowrun-thumb__img flowrun-thumb__img--empty'))
      }
      const caption = el('figcaption', 'flowrun-thumb__name', label)
      caption.title = label
      figure.append(caption)
      grid.append(figure)
    })
    const more = card.images.length - FLOW_RUN_MAX_THUMBS
    if (more > 0)
      grid.append(el('span', 'flowrun-card__more', t('chat.flowRunMore', { count: String(more) })))
    root.append(grid)
  } else if (isTerminalFlowRun(card)) {
    root.append(el('p', 'flowrun-card__empty', t('chat.flowRunNoImages')))
  }

  if (card.error) {
    root.append(el('p', 'flowrun-card__error', card.error.message))
  }

  if (options.openFlow && card.flowId) {
    const foot = el('div', 'flowrun-card__foot')
    const open = el('button', 'flowrun-card__open', t('chat.flowRunOpen'))
    open.type = 'button'
    open.setAttribute('data-flowrun-action', 'open')
    open.setAttribute('data-flowrun-flow', card.flowId)
    foot.append(open)
    root.append(foot)
  }

  body.replaceChildren(root)
  return root
}

/* ── Mounter ────────────────────────────────────────────────────────────── */

export interface FlowRunMounterDeps {
  /** Fetch a flow-run artifact body from its (authenticated) URL. */
  fetchPayload: (url: string) => Promise<unknown>
  /** The gateway call for `flows.run.get` and `flows.media` (both reads). */
  call?: FlowRunCall | null
  /**
   * "Open in Flows": a getter read at render and click time, so the handler
   * may come and go without a remount. Absent, or answering null → no button.
   */
  openFlow?: (() => FlowRunOpen | null | undefined) | null
  /** Timers (tests). Default: setTimeout / clearTimeout. */
  setTimer?: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>
  clearTimer?: (id: ReturnType<typeof setTimeout>) => void
  /** The diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/**
 * Create the flow-run card mounter. `mountFlowRuns(root)` is idempotent; a
 * card re-mounted by a transcript re-render draws the newest state this
 * mounter has seen for its run (it outlives `destroyAll`) and re-reads it.
 */
export function createFlowRunMounter(deps: FlowRunMounterDeps) {
  const diag = deps.diag ?? ((): void => {})
  const setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms))
  const clearTimer = deps.clearTimer ?? ((id) => clearTimeout(id))
  const claimed = new Set<HTMLElement>()
  const cards = new Map<HTMLElement, FlowRunCard>()
  /** Newest card per run id; survives re-mounts and destroyAll. */
  const latest = new Map<string, FlowRunCard>()
  /** Thumbnail data URLs by sha256 ('' = failed, do not retry). */
  const thumbs = new Map<string, string>()
  const thumbsInFlight = new Set<string>()
  const pendingRefresh = new Map<string, ReturnType<typeof setTimeout>>()
  const wired = new WeakSet<HTMLElement>()

  const getOpen = (): FlowRunOpen | null => {
    const value = deps.openFlow ? deps.openFlow() : null
    return typeof value === 'function' ? value : null
  }

  function hostsFor(runId: string): HTMLElement[] {
    return [...cards.entries()].filter(([, c]) => c.runId === runId).map(([h]) => h)
  }

  function draw(host: HTMLElement, card: FlowRunCard): void {
    cards.set(host, card)
    renderFlowRun(host, card, {
      thumb: (sha) => thumbs.get(sha) ?? '',
      openFlow: getOpen(),
    })
    loadThumbs(card)
  }

  function loadThumbs(card: FlowRunCard): void {
    const call = deps.call
    if (!call) return
    for (const img of card.images.slice(0, FLOW_RUN_MAX_THUMBS)) {
      if (thumbs.has(img.sha256) || thumbsInFlight.has(img.sha256)) continue
      thumbsInFlight.add(img.sha256)
      void call('flows.media', { sha256: img.sha256, max_side: FLOW_RUN_THUMB_SIDE })
        .then((raw) => thumbnailUrl(raw))
        .catch((error: unknown) => {
          diag('flowrun.thumb.error', { sha256: img.sha256, error: String(error) })
          return ''
        })
        .then((url) => {
          thumbsInFlight.delete(img.sha256)
          if (thumbs.size >= THUMB_CACHE_MAX) {
            const oldest = thumbs.keys().next().value
            if (oldest !== undefined) thumbs.delete(oldest)
          }
          thumbs.set(img.sha256, url)
          if (!url) return
          for (const [host, shown] of cards) {
            if (!host.isConnected) continue
            if (shown.images.some((i) => i.sha256 === img.sha256)) draw(host, shown)
          }
        })
    }
  }

  function remember(card: FlowRunCard): FlowRunCard {
    const known = latest.get(card.runId)
    // A live read wins over a snapshot; keep the snapshot's flow name.
    const merged = { ...card, flowName: card.flowName || known?.flowName || card.flowId }
    latest.set(card.runId, merged)
    return merged
  }

  async function refresh(runId: string): Promise<void> {
    const call = deps.call
    if (!call) return
    try {
      const raw = obj(await call('flows.run.get', { run_id: runId }))
      const known = latest.get(runId)
      const card = cardFromRun(raw?.run, known?.flowName ?? '')
      if (!card) return
      const merged = remember(card)
      for (const host of hostsFor(runId)) {
        if (host.isConnected) draw(host, merged)
      }
      diag('flowrun.refresh.done', { runId, status: merged.status })
    } catch (error) {
      // The snapshot stays; a run the gateway no longer has is still a record.
      diag('flowrun.refresh.error', { runId, error: String(error) })
    }
  }

  function setStatus(host: HTMLElement, message: string): void {
    const node = host.querySelector<HTMLElement>('.msg-artifact-flowrun__status')
    if (!node) return
    node.textContent = message
    node.hidden = message === ''
  }

  function wire(host: HTMLElement): void {
    if (wired.has(host)) return
    wired.add(host)
    host.addEventListener('click', (event) => {
      const button = (event.target as HTMLElement | null)?.closest<HTMLElement>(
        '[data-flowrun-action="open"]',
      )
      if (!button || !host.contains(button)) return
      event.preventDefault()
      const flowId = button.getAttribute('data-flowrun-flow') || ''
      const open = getOpen()
      if (open && flowId) open(flowId)
    })
  }

  async function mountOne(host: HTMLElement): Promise<void> {
    const url = host.dataset.flowrunSrc || ''
    if (!url) {
      setStatus(host, t('chat.flowRunUnavailable'))
      return
    }
    try {
      const raw = await deps.fetchPayload(url)
      const snapshot = normalizeFlowRunPayload(raw)
      if (!snapshot) {
        setStatus(host, t('chat.flowRunUnreadable'))
        return
      }
      if (!host.isConnected || !claimed.has(host)) {
        claimed.delete(host)
        return
      }
      const known = latest.get(snapshot.runId)
      const shown = known ?? remember(snapshot)
      wire(host)
      draw(host, shown)
      setStatus(host, '')
      diag('flowrun.mount.done', { runId: shown.runId, cached: Boolean(known) })
      // Always re-read once: a snapshot (or a card from before a reconnect)
      // may be behind the run.
      void refresh(shown.runId)
    } catch (error) {
      setStatus(host, t('chat.flowRunFailed'))
      diag('flowrun.mount.error', { url, error: String(error) })
    }
  }

  function pruneDetached(): void {
    for (const host of [...claimed]) {
      if (!host.isConnected) {
        claimed.delete(host)
        cards.delete(host)
      }
    }
  }

  /** Mount every not-yet-mounted flow-run placeholder inside `root`. */
  function mountFlowRuns(root: HTMLElement | null | undefined): void {
    if (!root) return
    pruneDetached()
    root.querySelectorAll<HTMLElement>('[data-flowrun-src]').forEach((host) => {
      if (claimed.has(host)) return
      claimed.add(host)
      void mountOne(host)
    })
  }

  /**
   * A `flows.run` / `flows.node` event: re-read a mounted, unfinished run
   * (folded per run), at once when the run itself reports an end.
   */
  function runEvent(payload: unknown): void {
    const row = obj(payload)
    const runId = text(row?.run_id)
    if (!runId || hostsFor(runId).length === 0) return
    const known = latest.get(runId)
    const ending = TERMINAL.has(status(row?.status)) && row?.node_id === undefined
    if (known && isTerminalFlowRun(known) && !ending) return
    const queued = pendingRefresh.get(runId)
    if (queued !== undefined) {
      if (!ending) return
      clearTimer(queued)
    }
    pendingRefresh.set(
      runId,
      setTimer(
        () => {
          pendingRefresh.delete(runId)
          void refresh(runId)
        },
        ending ? 0 : FLOW_RUN_REFRESH_MS,
      ),
    )
  }

  /** Re-read every mounted unfinished run (after a reconnect). */
  function reconnected(): void {
    const runs = new Set(
      [...cards.values()].filter((c) => !isTerminalFlowRun(c)).map((c) => c.runId),
    )
    runs.forEach((runId) => void refresh(runId))
  }

  /** Forget every host and timer (route unmount). Run state and thumbnails stay. */
  function destroyAll(): void {
    claimed.clear()
    cards.clear()
    pendingRefresh.forEach((id) => clearTimer(id))
    pendingRefresh.clear()
  }

  return { mountFlowRuns, runEvent, reconnected, destroyAll, pruneDetached, refresh }
}

export type FlowRunMounter = ReturnType<typeof createFlowRunMounter>
