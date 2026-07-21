// Chat transcript — tool-activity + subagent-disclosure imperative renderer.
//
// This module is part of the OWNER-APPROVED imperative boundary of the
// chat-view migration (design §2.1): the tool-card / subagent region is ported
// as near-verbatim imperative DOM code (createElement/appendChild/innerHTML)
// from static/js/views/chat.js, NOT reactified. Each function carries the cited
// legacy line range it was ported from.
//
// Split into two surfaces:
//   1. Pure helpers (top-level exports) — no DOM, no controller state — that
//      are unit-tested in isolation (tools.test.ts). These are the sanctioned
//      test surface for this task.
//   2. `createToolRenderer(deps)` — a factory the streaming controller composes.
//      The DOM builders need controller-internal state (the streaming bubble,
//      the segment list, auto-scroll, scroll-to-bottom); those are injected as
//      `deps` so the legacy module-globals rebind to the SAME controller fields
//      the streaming path mutates. DOM behavior is verified by a live-browser
//      sweep (parity matrix), not RTL.

import type { StreamEventPayload } from '../types'

/* ── Constants (ported verbatim from chat.js) ───────────────────────────── */

// chat.js:503-516 — tool-name → emoji icon map.
const TOOL_EMOJI: Record<string, string> = {
  bash: '💻', // 💻
  read_file: '📄', // 📄
  write_file: '✏️', // ✏️
  edit_file: '✏️', // ✏️
  web_search: '🔍', // 🔍
  search: '🔍', // 🔍
  http_request: '🌐', // 🌐
  web_fetch: '🌐', // 🌐
  list_files: '📂', // 📂
  memory_search: '🧠', // 🧠
  memory_store: '🧠', // 🧠
}

// chat.js:517-519 — emoji for a tool name, ⚡ default.
export function toolEmoji(name: string): string {
  return TOOL_EMOJI[name] || '⚡'
}

// chat.js:444 — provider → logo map used on the web_search badge.
const PROVIDER_LOGOS: Record<string, string> = {
  brave: '🦁', // 🦁
  duckduckgo: '🦆', // 🦆
}

/* ── Pure helpers (unit-tested) ─────────────────────────────────────────── */

// chat.js:953-956 — truncate with a single-char ellipsis suffix.
function truncate(s: string, max = 200): string {
  if (!s || s.length <= max) return s || ''
  return s.slice(0, max) + '…'
}

// chat.js:661-667 — minimal HTML-entity escape.
function esc(s: string): string {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// chat.js:7022-7034 — coerce a tool input into a plain object (parse a JSON
// string that starts with `{`), else null.
function toolInputObject(input: unknown): Record<string, unknown> | null {
  if (!input) return null
  if (typeof input === 'object') return input as Record<string, unknown>
  if (typeof input !== 'string') return null
  const trimmed = input.trim()
  if (!trimmed || !trimmed.startsWith('{')) return null
  try {
    const parsed = JSON.parse(trimmed)
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null
  } catch {
    return null
  }
}

// chat.js:7036-7041 — the trailing path segment.
function basename(path: unknown): string {
  const raw = String(path || '').trim()
  if (!raw) return ''
  const parts = raw.split(/[\\/]+/).filter(Boolean)
  return parts.length ? (parts[parts.length - 1] as string) : raw
}

// chat.js:7043-7047 — publish_artifact target basename (name || path).
// Exported so the artifact renderer (artifacts.ts) reuses this one definition
// rather than duplicating it (DRY — it is the SAME legacy `_publishArtifactTargetName`).
export function publishArtifactTargetName(input: unknown): string {
  const obj = toolInputObject(input)
  if (!obj) return ''
  return basename(obj.name || obj.path)
}

// chat.js:7049-7055 — display name; publish_artifact appends the target basename.
export function toolDisplayName(name: string, input: unknown): string {
  if (name === 'publish_artifact') {
    const target = publishArtifactTargetName(input)
    if (target) return `${name} - ${target}`
  }
  return name || 'tool'
}

// chat.js:7057-7059 — control-plane tools are hidden from the transcript.
export function isControlPlaneToolName(name: string): boolean {
  return name === 'router_control'
}

// chat.js:7107-7113 — human duration. NOTE: there is no "ms" branch in the
// legacy source; sub-second values render as `0.Ns` via `.toFixed(1)`.
export function fmtToolDuration(ms: number): string {
  if (!ms || ms < 0) return ''
  const s = ms / 1000
  if (s < 10) return `${s.toFixed(1)}s`
  if (s < 60) return `${Math.round(s)}s`
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
}

// chat.js:7201-7204 — the execution_status object (accepts snake/camel).
function toolExecutionStatus(payload: StreamEventPayload): Record<string, unknown> | null {
  const p = payload as { execution_status?: unknown; executionStatus?: unknown } | undefined
  const status = p && (p.execution_status || p.executionStatus)
  return status && typeof status === 'object' ? (status as Record<string, unknown>) : null
}

// chat.js:7206-7212 — error predicate: execution_status.status ∈
// {error,timeout,cancelled}, else the is_error/isError/error flags.
export function toolResultIsError(payload: StreamEventPayload): boolean {
  const status = toolExecutionStatus(payload)
  if (status && typeof status.status === 'string') {
    return ['error', 'timeout', 'cancelled'].includes(status.status)
  }
  const p = payload as { is_error?: unknown; isError?: unknown; error?: unknown } | undefined
  return !!(p && (p.is_error || p.isError || p.error))
}

// chat.js:7214-7219 — the settled-state CSS class.
function toolResultStateClass(payload: StreamEventPayload): string {
  const status = toolExecutionStatus(payload)
  if (status && status.status === 'success') return 'chat-tools-collapse--success'
  if (status && status.status === 'unknown') return 'chat-tools-collapse--unknown'
  return toolResultIsError(payload) ? 'chat-tools-collapse--error' : 'chat-tools-collapse--success'
}

// chat.js:7221-7224 — truncation flag lives on execution_status.truncated.
export function toolResultIsTruncated(payload: StreamEventPayload): boolean {
  const status = toolExecutionStatus(payload)
  return !!(status && status.truncated)
}

// chat.js:7226-7239 — the displayable result content (result | content | output),
// JSON-stringified when non-string.
function toolResultContent(payload: StreamEventPayload): string {
  if (!payload) return ''
  let raw: unknown = ''
  if (Object.prototype.hasOwnProperty.call(payload, 'result')) {
    raw = (payload as { result?: unknown }).result
  } else if (Object.prototype.hasOwnProperty.call(payload, 'content')) {
    raw = (payload as { content?: unknown }).content
  } else if (Object.prototype.hasOwnProperty.call(payload, 'output')) {
    raw = (payload as { output?: unknown }).output
  }
  if (typeof raw === 'string') return raw
  const rendered = JSON.stringify(raw, null, 2)
  return rendered == null ? '' : rendered
}

// chat.js:480-493 — resolve the search provider from a payload/segment or the
// JSON-ish result content.
function toolResultProvider(payloadOrSegment: StreamEventPayload, content: string): string {
  const p = payloadOrSegment as
    { provider?: unknown; search_provider?: unknown; searchProvider?: unknown } | undefined
  const direct = p?.provider || p?.search_provider || p?.searchProvider
  if (direct) return String(direct)
  if (!content) return ''
  try {
    const parsed = JSON.parse(content) as { provider?: unknown }
    return parsed.provider ? String(parsed.provider) : ''
  } catch {
    const match = String(content).match(/"provider"\s*:\s*"([^"]+)"/)
    return match ? (match[1] ?? '') : ''
  }
}

// chat.js:7817-7825 — parse a subagent_completion payload, else null.
export function parseSubagentCompletion(
  text: string,
): (Record<string, unknown> & { type: 'subagent_completion' }) | null {
  try {
    const parsed = JSON.parse(text) as { type?: unknown }
    if (parsed && parsed.type === 'subagent_completion') {
      return parsed as Record<string, unknown> & { type: 'subagent_completion' }
    }
  } catch {
    // Not a subagent completion payload.
  }
  return null
}

// chat.js:7241-7258 — parse memory_search source rows (max 6).
interface MemorySourceRow {
  index: string
  path: string
  source: string
  lines: string
  citation: string
}
function memorySearchSourceRows(content: string): MemorySourceRow[] {
  if (!content || typeof content !== 'string') return []
  const rows: MemorySourceRow[] = []
  const pattern =
    /^\[(\d+)\]\s+(.+?)\s+\(source:\s*([^;]+);\s*lines\s+([^;]+);\s*citation:\s*([^;]+);/
  for (const line of content.split('\n')) {
    const match = line.match(pattern)
    if (!match) continue
    rows.push({
      index: match[1] ?? '',
      path: match[2] ?? '',
      source: match[3] ?? '',
      lines: match[4] ?? '',
      citation: match[5] ?? '',
    })
    if (rows.length >= 6) break
  }
  return rows
}

// chat.js:7260-7282 — memory_search source-badge DOM.
function buildMemorySearchSourceDOM(content: string): HTMLElement | null {
  const rows = memorySearchSourceRows(content)
  if (!rows.length) return null

  const wrap = document.createElement('div')
  wrap.className = 'chat-memory-sources'
  for (const row of rows) {
    const item = document.createElement('div')
    item.className = 'chat-memory-source'

    const badge = document.createElement('span')
    badge.className = 'chat-memory-source-badge chat-memory-source-badge--' + row.source
    badge.textContent = row.source
    item.appendChild(badge)

    const cite = document.createElement('span')
    cite.className = 'chat-memory-source-citation'
    cite.textContent = row.citation || row.path + '#L' + row.lines
    item.appendChild(cite)
    wrap.appendChild(item)
  }
  return wrap
}

/* ── Summary-status helpers (chat.js:7115-7141) ─────────────────────────── */

// chat.js:7115-7119 — running renders no text; settled rows show a duration.
function visibleToolSummaryStatus(status: string, durationMs: number): string {
  if (status === 'success' || status === 'error') return fmtToolDuration(durationMs)
  return ''
}

// chat.js:7121-7128 — write the status glyph state + visible text onto the span.
function applyToolSummaryStatus(statusSpan: HTMLElement, status: string, durationMs = 0): void {
  const visibleStatus = visibleToolSummaryStatus(status || '', durationMs | 0)
  statusSpan.dataset.status = status || ''
  statusSpan.textContent = visibleStatus
  statusSpan.hidden = false
}

// chat.js:7130-7141 — ensure + update the summary status span on a details el.
function setToolSummaryStatus(details: HTMLElement | null, status: string, durationMs = 0): void {
  if (!details) return
  const summary = details.querySelector('.chat-tools-summary')
  if (!summary) return
  let statusSpan = summary.querySelector<HTMLElement>('.chat-tools-status')
  if (!statusSpan) {
    statusSpan = document.createElement('span')
    statusSpan.className = 'chat-tools-status'
    summary.appendChild(statusSpan)
  }
  applyToolSummaryStatus(statusSpan, status || '', durationMs | 0)
}

/* ── Provider-badge helpers (chat.js:446-478) ───────────────────────────── */

// chat.js:446-448 — trim the provider label.
function normalizeProvider(provider: unknown): string {
  return String(provider || '').trim()
}

// chat.js:450-461 — inject / update the search-provider badge on a summary.
function injectProviderBadge(summary: Element | null, providerRaw: string): void {
  const provider = normalizeProvider(providerRaw)
  if (!summary || !provider) return
  let badge = summary.querySelector<HTMLElement>('.chat-tool-provider')
  if (!badge) {
    badge = document.createElement('span')
    badge.className = 'chat-tool-provider'
    summary.appendChild(badge)
  }
  badge.textContent = (PROVIDER_LOGOS[provider] || '') + ' ' + provider
  badge.title = 'Search provider: ' + provider
}

/* ── DOM lookup helpers (chat.js:7167-7179) ─────────────────────────────── */

// chat.js:7167-7172 — find a tool <details> by tool_use_id under a root.
function findToolDetailsById(root: Element | null, toolId: string): HTMLElement | null {
  if (!root || !toolId) return null
  return (
    Array.from(root.querySelectorAll<HTMLElement>('[data-tool-id]')).find(
      (el) => el.getAttribute('data-tool-id') === toolId,
    ) || null
  )
}

// chat.js:7174-7179 — find a rendered tool-result block by tool_use_id.
function findToolResultById(root: Element | null, toolId: string): HTMLElement | null {
  if (!root || !toolId) return null
  return (
    Array.from(root.querySelectorAll<HTMLElement>('[data-tool-result-for]')).find(
      (el) => el.getAttribute('data-tool-result-for') === toolId,
    ) || null
  )
}

/* ── Injected controller dependencies ───────────────────────────────────── */

/**
 * The controller-internal surface the DOM builders bind against. These rebind
 * the legacy module-globals to the SAME fields the streaming path mutates, so
 * tool cards land inside the live streaming bubble and share its segment list /
 * auto-scroll. Every accessor maps to an existing controller field or method.
 */
export interface ToolRendererDeps {
  /** chat.js `_ensureStreamBubble` (stream.ts ensureStreamBubble). */
  ensureStreamBubble: () => HTMLElement
  /** chat.js `_markVisibleStreamEvent` (stream.ts markVisibleStreamEvent). */
  markVisibleStreamEvent: (kind: string) => void
  /** chat.js `_flushPendingTextSegment` (stream.ts flushPendingTextSegment). */
  flushPendingTextSegment: () => void
  /** chat.js `_newTextSegment` (stream.ts newTextSegment). */
  newTextSegment: () => HTMLElement
  /** chat.js `_scrollToBottom` (stream.ts scrollToBottom). */
  scrollToBottom: () => void
  /** chat.js `_autoScroll` (stream.ts _autoScroll field). */
  getAutoScroll: () => boolean
  /** chat.js `_segments.push` — append a tool segment to the live list. */
  pushSegment: (seg: { type: string; el: HTMLElement }) => void
  /** chat.js `_searchProvider` — the sticky web_search provider (read). */
  getSearchProvider: () => string
  /** chat.js `_setSearchProvider` — remember the resolved provider. */
  setSearchProvider: (provider: string, options?: { refreshRunning?: boolean }) => void
  /** chat.js `_sessionKey` — the active session (subagent parent-session gate). */
  getSessionKey: () => string
  /** chat.js `_addMessage` — append a plain (non-streaming) message row. */
  addMessage: (
    role: string,
    text: string,
    timestamp: number,
    options: Record<string, unknown>,
  ) => HTMLElement | null
  /** chat.js `_messages.push` — record the message for export/reconcile. */
  pushMessage: (message: Record<string, unknown>) => void
  /** chat.js `UI.modal` — open the "View full" result modal. Default: no-op. */
  openModal?: (title: string, html: string, buttons: Array<Record<string, unknown>>) => void
  /** chat.js `_chatDiag` — the diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/* ── Factory ────────────────────────────────────────────────────────────── */

/**
 * Create the tool-activity renderer bound to controller-internal state. The
 * streaming controller composes this and re-exports `appendToolCall` /
 * `appendToolResult` / `settleToolResultCard` / `reconstructToolCalls` /
 * `appendSubagentCompletion` so `useTranscript` can wire the seams to them.
 */
export function createToolRenderer(deps: ToolRendererDeps) {
  const diag = deps.diag ?? (() => {})
  const openModal = deps.openModal ?? (() => {})

  /* ── retitle (chat.js:7143-7165) ──────────────────────────────────────── */

  function retitleToolCallDOM(details: HTMLElement | null, name: string, input: unknown): void {
    if (!details || !name) return
    const current = details.getAttribute('data-tool-name') || ''
    if (current === name) return
    details.setAttribute('data-tool-name', name)
    const summary = details.querySelector('.chat-tools-summary')
    if (!summary) return
    const providerBadge = summary.querySelector('.chat-tool-provider')
    if (providerBadge) providerBadge.remove()
    const currentStatus = summary.querySelector<HTMLElement>('.chat-tools-status')
    const statusText = currentStatus?.dataset?.status || currentStatus?.textContent || ''
    summary.textContent = ''
    const iconSpan = document.createElement('span')
    iconSpan.className = 'chat-tools-icon'
    iconSpan.textContent = toolEmoji(name)
    summary.appendChild(iconSpan)
    summary.appendChild(document.createTextNode(' ' + toolDisplayName(name, input)))
    const statusSpan = document.createElement('span')
    statusSpan.className = 'chat-tools-status'
    applyToolSummaryStatus(statusSpan, statusText)
    summary.appendChild(statusSpan)
    if (providerBadge) summary.appendChild(providerBadge)
  }

  /* ── build tool-call DOM (chat.js:7061-7105) ──────────────────────────── */

  function buildToolCallDOM(
    name: string,
    toolId: string,
    input: unknown,
    isRunning: boolean,
  ): HTMLElement {
    const displayName = toolDisplayName(name, input)
    const preview = truncate(
      typeof input === 'string' ? input : JSON.stringify(input || '', null, 2),
      200,
    )

    const details = document.createElement('details')
    details.className = 'chat-tools-collapse' + (isRunning ? ' chat-tools-collapse--running' : '')
    if (toolId) details.setAttribute('data-tool-id', toolId)
    details.setAttribute('data-tool-name', name || 'tool')
    if (isRunning) details.dataset.startedAt = String(Date.now())

    const summary = document.createElement('summary')
    summary.className = 'chat-tools-summary'
    if (isRunning) summary.setAttribute('aria-disabled', 'true')
    // Block expansion while the tool is still running; cleared when state flips
    // to success/error.
    summary.addEventListener('click', (e) => {
      if (details.classList.contains('chat-tools-collapse--running')) e.preventDefault()
    })
    const iconSpan = document.createElement('span')
    iconSpan.className = 'chat-tools-icon'
    iconSpan.textContent = toolEmoji(name)
    summary.appendChild(iconSpan)
    summary.appendChild(document.createTextNode(' ' + displayName))
    const statusSpan = document.createElement('span')
    statusSpan.className = 'chat-tools-status'
    applyToolSummaryStatus(statusSpan, isRunning ? 'running' : '')
    summary.appendChild(statusSpan)

    const toolsBody = document.createElement('div')
    toolsBody.className = 'chat-tools-body'

    // Only show input preview if non-empty (arguments may arrive later via
    // tool_use_delta).
    const emptyInputs = ['', '""', '{}', 'null', 'undefined']
    if (preview && !emptyInputs.includes(preview.trim())) {
      const cardInput = document.createElement('div')
      cardInput.className = 'chat-tool-input'
      cardInput.textContent = preview
      toolsBody.appendChild(cardInput)
    }
    details.appendChild(summary)
    details.appendChild(toolsBody)
    return details
  }

  /* ── build tool-result DOM (chat.js:7284-7318) ────────────────────────── */

  function buildToolResultDOM(
    content: string,
    isError: boolean,
    isTruncated = false,
    toolName = '',
  ): HTMLElement | null {
    const preview = truncate(content, 200)
    if (!preview || preview.trim() === '') return null

    const div = document.createElement('div')
    div.className =
      'chat-tool-result' +
      (isError ? ' chat-tool-result--error' : '') +
      (isTruncated ? ' chat-tool-result--warn' : '')

    const previewDiv = document.createElement('div')
    previewDiv.className = 'chat-tool-result-preview'
    previewDiv.textContent = preview
    div.appendChild(previewDiv)

    if (toolName === 'memory_search') {
      const sources = buildMemorySearchSourceDOM(content)
      if (sources) div.appendChild(sources)
    }

    if (content.length > 200) {
      const viewBtn = document.createElement('button')
      viewBtn.className = 'btn btn--sm btn--ghost chat-tool-view-btn'
      viewBtn.type = 'button'
      viewBtn.textContent = 'View full'
      viewBtn.addEventListener('click', (event) => {
        event.preventDefault()
        event.stopPropagation()
        openModal('Tool Result', '<pre class="chat-tool-result-full">' + esc(content) + '</pre>', [
          { label: 'Close', cls: 'btn-secondary' },
        ])
      })
      div.appendChild(viewBtn)
    }
    return div
  }

  /* ── settle a result card (chat.js:7181-7199) ─────────────────────────── */

  function settleToolResultCard(payload: StreamEventPayload, isError: boolean): HTMLElement | null {
    const toolId = (payload && (payload as { tool_use_id?: string }).tool_use_id) || ''
    if (!toolId) return null
    const bubble = deps.ensureStreamBubble()
    const body = bubble && bubble.querySelector('.msg-body')
    const details = findToolDetailsById(body, toolId)
    if (!details) return null
    let toolName =
      (payload as { name?: string; tool_name?: string }).name ||
      (payload as { tool_name?: string }).tool_name ||
      ''
    if (toolName) {
      retitleToolCallDOM(
        details,
        toolName,
        (payload as { arguments?: unknown; input?: unknown }).arguments ||
          (payload as { input?: unknown }).input ||
          '',
      )
    }
    toolName = toolName || details.getAttribute('data-tool-name') || ''
    details.classList.remove('chat-tools-collapse--running')
    details.classList.add(toolResultStateClass(payload))
    const startedAt = Number(details.dataset.startedAt || 0)
    const elapsedMs = startedAt ? Date.now() - startedAt : 0
    setToolSummaryStatus(details, isError ? 'error' : 'success', elapsedMs)
    const summary = details.querySelector('.chat-tools-summary')
    if (summary) summary.removeAttribute('aria-disabled')
    return details
  }

  /* ── append a tool call (chat.js:7320-7366) ───────────────────────────── */

  function appendToolCall(payload: StreamEventPayload): void {
    if (!payload) return
    diag('tool_call.append.start', {})
    const name =
      (payload as { name?: string; tool_name?: string }).name ||
      (payload as { tool_name?: string }).tool_name ||
      'tool'
    if (isControlPlaneToolName(name)) {
      diag('tool_call.append.skip_control_plane', { name })
      return
    }
    const rawInput = (payload as { input?: unknown; arguments?: unknown }).input
    const input =
      typeof rawInput === 'string'
        ? rawInput
        : JSON.stringify(rawInput || (payload as { arguments?: unknown }).arguments || '', null, 2)
    const toolId = (payload as { tool_use_id?: string }).tool_use_id || ''

    const bubble = deps.ensureStreamBubble()
    deps.markVisibleStreamEvent('tool_use_start')
    const body = bubble.querySelector('.msg-body')
    const existing = findToolDetailsById(body, toolId)
    if (existing) {
      diag('tool_call.append.reuse_existing', { toolId, name })
      const provider = deps.getSearchProvider()
      if (name === 'web_search' && provider) {
        injectProviderBadge(existing.querySelector('.chat-tools-summary'), provider)
      }
      if (deps.getAutoScroll()) deps.scrollToBottom()
      return
    }

    const details = buildToolCallDOM(name, toolId, input, true)
    const provider = deps.getSearchProvider()
    if (name === 'web_search' && provider) {
      injectProviderBadge(details.querySelector('.chat-tools-summary'), provider)
    }
    deps.flushPendingTextSegment()
    if (body) body.appendChild(details)
    deps.pushSegment({ type: 'tool', el: details })

    // Seal the current text segment and start a new one for text after this
    // tool call.
    deps.newTextSegment()

    if (deps.getAutoScroll()) deps.scrollToBottom()
    diag('tool_call.append.done', { toolId, name })
  }

  /* ── append a tool result (chat.js:7368-7448) ─────────────────────────── */

  function appendToolResult(payload: StreamEventPayload): void {
    if (!payload) return
    diag('tool_result.append.start', {})

    const content = toolResultContent(payload)
    const isError = toolResultIsError(payload)
    const toolId = (payload as { tool_use_id?: string }).tool_use_id || ''
    let toolName =
      (payload as { name?: string; tool_name?: string }).name ||
      (payload as { tool_name?: string }).tool_name ||
      ''
    if (isControlPlaneToolName(toolName)) {
      diag('tool_result.append.skip_control_plane', { toolId, toolName })
      return
    }

    const bubble = deps.ensureStreamBubble()
    deps.markVisibleStreamEvent('tool_result')
    const body = bubble.querySelector('.msg-body')

    // Transition tool container from running → success/error and find target
    // container.
    let resultTarget: Element | null = body // default: append to msg-body
    if (toolId) {
      const details = findToolDetailsById(body, toolId)
      if (details) {
        if (toolName) {
          retitleToolCallDOM(
            details,
            toolName,
            (payload as { arguments?: unknown; input?: unknown }).arguments ||
              (payload as { input?: unknown }).input ||
              '',
          )
        }
        toolName = toolName || details.getAttribute('data-tool-name') || ''
        details.classList.remove('chat-tools-collapse--running')
        details.classList.add(toolResultStateClass(payload))
        const startedAt = Number(details.dataset.startedAt || 0)
        const elapsedMs = startedAt ? Date.now() - startedAt : 0
        setToolSummaryStatus(details, isError ? 'error' : 'success', elapsedMs)
        const summary = details.querySelector('.chat-tools-summary')
        if (summary) summary.removeAttribute('aria-disabled')
        const toolsBody = details.querySelector('.chat-tools-body')
        if (toolsBody) resultTarget = toolsBody

        // web_search: add provider badge to collapsible summary (may already be
        // present from running state).
        if (toolName === 'web_search') {
          const provider = toolResultProvider(payload, content)
          if (provider) {
            deps.setSearchProvider(provider, { refreshRunning: false })
            injectProviderBadge(details.querySelector('.chat-tools-summary'), provider)
          }
        }
      }
    }
    if (toolId && findToolResultById(resultTarget, toolId)) {
      if (deps.getAutoScroll()) deps.scrollToBottom()
      diag('tool_result.append.skip_duplicate', { toolId, toolName })
      return
    }

    // Only show result preview if non-empty.
    const resultDiv = buildToolResultDOM(content, isError, toolResultIsTruncated(payload), toolName)
    if (!resultDiv) {
      if (deps.getAutoScroll()) deps.scrollToBottom()
      diag('tool_result.append.skip_empty_result', { toolId, toolName })
      return
    }

    if (toolId) resultDiv.setAttribute('data-tool-result-for', toolId)
    if (resultTarget) resultTarget.appendChild(resultDiv)
    if (deps.getAutoScroll()) deps.scrollToBottom()
    diag('tool_result.append.done', { toolId, toolName })
  }

  /* ── reconstruct tool calls from persisted segments (chat.js:7681-7758) ── */

  function reconstructToolCalls(
    bubbleDiv: HTMLElement,
    segments: Array<Record<string, unknown>>,
    render: {
      renderText: (text: string, into: HTMLElement) => void
      stripText: (text: string) => string
    },
  ): void {
    try {
      const body = bubbleDiv.querySelector('.msg-body')
      if (!body) return

      // Clear existing text content (will be re-rendered from segments).
      body.innerHTML = ''

      // Build tool_use_id → tool name/input maps so tool_result segments can
      // look up the name.
      const toolNameById: Record<string, string> = {}
      const toolInputById: Record<string, unknown> = {}
      for (const seg of segments) {
        if (seg.type === 'tool_use' && seg.tool_use_id) {
          toolNameById[String(seg.tool_use_id)] = (seg.name as string) || 'tool'
          toolInputById[String(seg.tool_use_id)] = seg.input || null
        }
      }

      for (const seg of segments) {
        if (seg.type === 'text') {
          const text = render.stripText((seg.text as string) || '').trim()
          if (!text) continue
          const textDiv = document.createElement('div')
          textDiv.className = 'msg-text-seg'
          render.renderText(text, textDiv)
          body.appendChild(textDiv)
        } else if (seg.type === 'tool_use') {
          if (isControlPlaneToolName((seg.name as string) || '')) continue
          if (findToolDetailsById(body, (seg.tool_use_id as string) || '')) continue
          const details = buildToolCallDOM(
            (seg.name as string) || 'tool',
            (seg.tool_use_id as string) || '',
            seg.input || '',
            false,
          )
          ;(details as unknown as { _agentosToolInput?: unknown })._agentosToolInput =
            seg.input || null
          body.appendChild(details)
        } else if (seg.type === 'tool_result') {
          const toolId = (seg.tool_use_id as string) || ''
          const isError = toolResultIsError(seg as StreamEventPayload)
          const content = toolResultContent(seg as StreamEventPayload)
          const resultToolName = (seg.name as string) || toolNameById[toolId] || ''
          if (isControlPlaneToolName(resultToolName)) continue

          if (toolId) {
            const details = findToolDetailsById(body, toolId)
            if (details) {
              retitleToolCallDOM(details, resultToolName, seg.input || '')
              const withInput = details as unknown as { _agentosToolInput?: unknown }
              withInput._agentosToolInput =
                withInput._agentosToolInput || toolInputById[toolId] || null
              details.classList.remove('chat-tools-collapse--running')
              details.classList.add(toolResultStateClass(seg as StreamEventPayload))
              const toolsBody = details.querySelector('.chat-tools-body')
              const resultTarget = toolsBody || details
              if (findToolResultById(resultTarget, toolId)) continue
              const resultDiv = buildToolResultDOM(
                content,
                isError,
                toolResultIsTruncated(seg as StreamEventPayload),
                resultToolName,
              )
              if (resultDiv) {
                resultDiv.setAttribute('data-tool-result-for', toolId)
                resultTarget.appendChild(resultDiv)
              }

              // web_search: inject provider badge + seed provider from the
              // persisted result.
              if (resultToolName === 'web_search' && content) {
                const provider = toolResultProvider(seg as StreamEventPayload, content)
                if (provider) {
                  deps.setSearchProvider(provider, { refreshRunning: false })
                  injectProviderBadge(details.querySelector('.chat-tools-summary'), provider)
                }
              }
            }
          }
        }
      }
    } catch {
      // Graceful degradation: leave original rendered content intact.
    }
  }

  /* ── subagent completion (chat.js:7796-7815) ──────────────────────────── */

  function appendSubagentCompletion(payload: StreamEventPayload): void {
    if (!payload) return
    const parentSession =
      (payload as { parent_session_key?: string }).parent_session_key ||
      (payload as { parentSessionKey?: string }).parentSessionKey ||
      ''
    const sessionKey = deps.getSessionKey()
    if (parentSession && sessionKey && parentSession !== sessionKey) return

    const text = JSON.stringify(payload)
    const timestamp = Date.now()
    const options = {
      provenanceKind: 'internal_system',
      provenanceSourceSessionKey:
        (payload as { child_session_key?: string }).child_session_key ||
        (payload as { childSessionKey?: string }).childSessionKey ||
        '',
      provenanceSourceTool: 'subagent_completion',
    }
    deps.pushMessage({ role: 'system', text, ts: timestamp, ...options })
    deps.addMessage('system', text, timestamp, options)
  }

  return {
    buildToolCallDOM,
    buildToolResultDOM,
    appendToolCall,
    appendToolResult,
    settleToolResultCard,
    reconstructToolCalls,
    appendSubagentCompletion,
    // Exposed for the controller's `_refreshRunningSearchProviderBadges`
    // (chat.js:463-469), which re-badges still-running web_search cards.
    injectProviderBadge: (summary: Element | null, provider: string): void =>
      injectProviderBadge(summary, provider),
  }
}

export type ToolRenderer = ReturnType<typeof createToolRenderer>
