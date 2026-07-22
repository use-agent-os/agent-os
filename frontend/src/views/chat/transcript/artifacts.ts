// Chat transcript — artifact cards + publish rendering (imperative).
//
// This module is part of the OWNER-APPROVED imperative boundary of the
// chat-view migration (design §2.1): the artifact-card region is ported as
// near-verbatim imperative DOM code (innerHTML string templates) from
// static/js/views/chat.js, NOT reactified. Each function carries the cited
// legacy line range it was ported from.
//
// Split into two surfaces (mirroring tools.ts):
//   1. Pure helpers (top-level exports) — no DOM, no controller state, no
//      module globals. The classification + URL builders. These are the
//      sanctioned unit-test surface for this task (artifacts.test.ts). The two
//      legacy URL builders that read `_sessionKey` + `App.getAuthToken()`
//      globals (`_artifactPreviewUrl`, `_artifactAuthenticatedDownloadUrl`) are
//      made pure here by taking `{ sessionKey, token }` explicitly; the DOM
//      factory below supplies those from injected deps.
//   2. `createArtifactRenderer(deps)` — a factory the streaming controller
//      composes (exactly how it composes `createToolRenderer`). The DOM builders
//      need controller-internal state (the streaming bubble, the stream-artifact
//      list, auto-scroll, scroll-to-bottom) + the session key / auth token /
//      toast, injected as `deps`. DOM behavior is verified by a live-browser
//      sweep (parity matrix), not RTL.
//
// The `publish_artifact` target-name helper (`_publishArtifactTargetName`,
// chat.js:7043) is NOT re-ported here — Task 4 already ported it into tools.ts;
// this module re-exports it from there so there is one definition (DRY).

export { publishArtifactTargetName } from './tools'

/* ── Artifact shape ─────────────────────────────────────────────────────── */

/** The artifact payload the gateway emits (open-ended; only cited fields used). */
export interface Artifact {
  id?: string
  name?: string
  mime?: string
  size?: number
  download_url?: string
  [k: string]: unknown
}

/* ── Classification maps (ported verbatim from chat.js:7494-7521) ───────── */

// chat.js:7494-7504
const ARTIFACT_MIME_CATEGORIES: Record<string, string> = {
  'application/json': 'data',
  'application/ndjson': 'data',
  'application/pdf': 'document',
  'application/x-ndjson': 'data',
  'text/csv': 'data',
  'text/html': 'document',
  'text/markdown': 'document',
  'text/plain': 'document',
  'text/tab-separated-values': 'data',
}

// chat.js:7506-7521
const ARTIFACT_EXTENSION_CATEGORIES: Record<string, string> = {
  csv: 'data',
  htm: 'document',
  html: 'document',
  ipynb: 'data',
  json: 'data',
  jsonl: 'data',
  log: 'document',
  markdown: 'document',
  md: 'document',
  ndjson: 'data',
  pdf: 'document',
  sql: 'code',
  tsv: 'data',
  txt: 'document',
}

// chat.js:7545 — audio extensions when the mime is empty/unknown.
const AUDIO_EXTENSIONS = ['mp3', 'wav', 'm4a', 'aac', 'ogg', 'oga', 'opus', 'flac', 'webm']

/* ── Pure helpers (unit-tested) ─────────────────────────────────────────── */

// chat.js:7523-7525 — lowercased mime, or "".
export function artifactMime(artifact: Artifact | null | undefined): string {
  return artifact && artifact.mime ? String(artifact.mime).toLowerCase() : ''
}

// chat.js:7527-7529 — the name, or the literal "artifact" fallback.
export function artifactName(artifact: Artifact | null | undefined): string {
  return artifact && artifact.name ? String(artifact.name) : 'artifact'
}

// chat.js:7531-7536 — lowercased extension; "" when no dot or a trailing dot.
export function artifactExtension(name: string): string {
  const trimmed = String(name || '')
    .trim()
    .toLowerCase()
  const idx = trimmed.lastIndexOf('.')
  if (idx < 0 || idx === trimmed.length - 1) return ''
  return trimmed.slice(idx + 1)
}

// chat.js:7538-7549 — category: visual | audio | data | document | code | file.
// NOTE: image/* maps to 'visual' (NOT 'image' — the brief example was wrong).
export function artifactCategory(artifact: Artifact | null | undefined): string {
  const mime = artifactMime(artifact)
  if (mime.startsWith('image/')) return 'visual'
  if (mime.startsWith('audio/')) return 'audio'
  if (ARTIFACT_MIME_CATEGORIES[mime]) return ARTIFACT_MIME_CATEGORIES[mime] as string
  if (!mime || mime === 'application/octet-stream' || mime === 'artifact') {
    const ext = artifactExtension(artifactName(artifact))
    if (AUDIO_EXTENSIONS.includes(ext)) return 'audio'
    if (ARTIFACT_EXTENSION_CATEGORIES[ext]) return ARTIFACT_EXTENSION_CATEGORIES[ext] as string
  }
  return 'file'
}

// chat.js:7551-7559 — category → chip glyph label.
export function artifactCategoryLabel(category: string): string {
  switch (category) {
    case 'data':
      return 'data'
    case 'document':
      return 'doc'
    case 'code':
      return 'code'
    case 'audio':
      return 'audio'
    default:
      return 'file'
  }
}

// chat.js:7561-7563 — image = the 'visual' category.
export function isImageArtifact(artifact: Artifact | null | undefined): boolean {
  return artifactCategory(artifact) === 'visual'
}

// chat.js:7565-7567 — audio = the 'audio' category.
export function isAudioArtifact(artifact: Artifact | null | undefined): boolean {
  return artifactCategory(artifact) === 'audio'
}

// chat.js:7480-7492 — the clean (session-key-stripped) relative download URL.
export function artifactDownloadUrl(artifact: Artifact | null | undefined): string {
  let raw = artifact && artifact.download_url ? String(artifact.download_url) : ''
  if (!raw && artifact && artifact.id) raw = `/api/v1/artifacts/${encodeURIComponent(artifact.id)}`
  if (!raw) return ''
  try {
    const url = new URL(raw, window.location.origin)
    url.searchParams.delete('sessionKey')
    url.searchParams.delete('session_key')
    return url.pathname + url.search + url.hash
  } catch {
    return raw
  }
}

/** The session/token context the two authenticated-URL builders need. */
export interface ArtifactUrlContext {
  sessionKey: string
  token: string
}

// chat.js:7569-7581 — inline preview URL (image/audio): the download URL with
// sessionKey + token appended. Legacy read `_sessionKey`/`App.getAuthToken()`
// globals; here they arrive via `ctx` so the helper stays pure.
export function artifactPreviewUrl(
  artifact: Artifact | null | undefined,
  ctx: ArtifactUrlContext,
): string {
  const raw = artifactDownloadUrl(artifact)
  if (!raw) return ''
  try {
    const url = new URL(raw, window.location.origin)
    if (ctx.sessionKey) url.searchParams.set('sessionKey', ctx.sessionKey)
    if (ctx.token) url.searchParams.set('token', ctx.token)
    return url.pathname + url.search + url.hash
  } catch {
    return raw
  }
}

// chat.js:7583-7593 — authenticated download href for a raw URL: append
// sessionKey + token. Same globals→ctx treatment as above.
export function artifactAuthenticatedDownloadUrl(raw: string, ctx: ArtifactUrlContext): string {
  if (!raw) return ''
  try {
    const url = new URL(raw, window.location.origin)
    if (ctx.sessionKey) url.searchParams.set('sessionKey', ctx.sessionKey)
    if (ctx.token) url.searchParams.set('token', ctx.token)
    return url.pathname + url.search + url.hash
  } catch {
    return raw
  }
}

/* ── Injected controller dependencies ───────────────────────────────────── */

/**
 * The controller-internal surface the DOM builders bind against. These rebind
 * the legacy module-globals to the SAME fields the streaming path mutates, so
 * artifact cards land inside the live streaming bubble and share its
 * auto-scroll + stream-artifact list. Every accessor maps to an existing
 * controller field/method.
 */
export interface ArtifactRendererDeps {
  /** chat.js `_ensureStreamBubble` (stream.ts ensureStreamBubble). */
  ensureStreamBubble: () => HTMLElement
  /** chat.js `_markVisibleStreamEvent` (stream.ts markVisibleStreamEvent). */
  markVisibleStreamEvent: (kind: string) => void
  /** chat.js `_scrollToBottom` (stream.ts scrollToBottom). */
  scrollToBottom: () => void
  /** chat.js `_autoScroll` (stream.ts _autoScroll field). */
  getAutoScroll: () => boolean
  /** chat.js `_streamBubble` — the live bubble (or null); read by renderStreamArtifacts. */
  getStreamBubble: () => HTMLElement | null
  /** chat.js `_streamArtifacts.push` — append to the live stream-artifact list. */
  pushStreamArtifact: (artifact: Artifact) => void
  /** chat.js `_streamArtifacts` — the live list, for renderStreamArtifacts. */
  getStreamArtifacts: () => Artifact[]
  /** chat.js `_sessionKey` — the active session (preview/download URL params). */
  getSessionKey: () => string
  /** chat.js `App.getAuthToken()` — the auth token (preview/download URL params). */
  getAuthToken: () => string
  /** chat.js `_esc` (logic.ts esc) — HTML-entity escape. */
  esc: (s: string) => string
  /** chat.js `_escAttr` — attribute escape (legacy delegates to `_esc`). Default: esc. */
  escAttr?: (s: string) => string
  /** chat.js `UI.toast` — failure toast for a bad download. Default: no-op. */
  toast?: (message: string, kind?: string, durationMs?: number) => void
  /** chat.js `_chatDiag` — the diagnostics ring. Default: no-op. */
  diag?: (event: string, detail: Record<string, unknown>) => void
}

/* ── Factory ────────────────────────────────────────────────────────────── */

/**
 * Create the artifact renderer bound to controller-internal state. The streaming
 * controller composes this and re-exports `appendArtifact` / `renderArtifacts`
 * / `renderStreamArtifacts` / `downloadArtifact` so `useTranscript` can wire the
 * `session.event.artifact` seam to `appendArtifact`, and so the reconcile /
 * park-restore path can call `renderStreamArtifacts` (mirroring the legacy
 * `_renderStreamArtifacts` dep the controller already declares).
 */
export function createArtifactRenderer(deps: ArtifactRendererDeps) {
  const esc = deps.esc
  const escAttr = deps.escAttr ?? deps.esc
  const toast = deps.toast ?? (() => {})
  const diag = deps.diag ?? (() => {})

  const urlContext = (): ArtifactUrlContext => ({
    sessionKey: deps.getSessionKey() || '',
    token: deps.getAuthToken() || '',
  })

  /* ── render artifact cards HTML (chat.js:7595-7651) ───────────────────── */

  function renderArtifacts(artifacts: Artifact[] | null | undefined): string {
    if (!Array.isArray(artifacts) || artifacts.length === 0) return ''
    let html = '<div class="msg-artifacts">'
    let openGroup = ''
    const { sessionKey, token } = urlContext()
    const closeGroup = (): void => {
      if (!openGroup) return
      html += '</div>'
      openGroup = ''
    }
    artifacts.forEach((artifact) => {
      const category = artifactCategory(artifact)
      const groupKind = category === 'visual' ? 'visual' : 'file'
      if (groupKind !== openGroup) {
        closeGroup()
        html +=
          groupKind === 'visual'
            ? '<div class="msg-artifact-gallery">'
            : '<div class="msg-artifact-files">'
        openGroup = groupKind
      }
      const name = artifactName(artifact)
      const mime = artifact && artifact.mime ? String(artifact.mime) : 'artifact'
      const size =
        artifact && artifact.size
          ? `${Math.max(1, Math.round(Number(artifact.size) / 1024))} KB`
          : ''
      const downloadUrl = artifactDownloadUrl(artifact || {})
      const downloadHref = artifactAuthenticatedDownloadUrl(downloadUrl, { sessionKey, token })
      const meta = [mime, size].filter(Boolean).join(' · ')
      if (isImageArtifact(artifact)) {
        const previewUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<a class="msg-artifact-card msg-artifact-card--image" href="${escAttr(downloadHref)}" download="${escAttr(name)}" data-artifact-category="${escAttr(category)}" data-artifact-download="${escAttr(downloadUrl)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}" title="Download ${escAttr(name)}">
          ${previewUrl ? `<img class="msg-artifact-preview" src="${esc(previewUrl)}" alt="${esc(name)}" loading="lazy">` : '<span class="msg-artifact-preview msg-artifact-preview--empty" aria-hidden="true"></span>'}
          <span class="msg-artifact-card__body">
            <span class="msg-artifact-card__name">${esc(name)}</span>
            <span class="msg-artifact-card__meta">${esc(meta)}</span>
          </span>
          <span class="msg-artifact-card__action" aria-hidden="true">Download</span>
        </a>`
      } else if (isAudioArtifact(artifact)) {
        html += `<div class="msg-artifact-card msg-artifact-card--audio" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <audio class="msg-artifact-audio" controls preload="metadata" src="${escAttr(downloadHref)}"></audio>
          <span class="msg-artifact-card__body">
            <span class="msg-artifact-card__name">${esc(name)}</span>
            <span class="msg-artifact-card__meta">${esc(meta)}</span>
          </span>
          <a class="msg-artifact-card__action" href="${escAttr(downloadHref)}" download="${escAttr(name)}" data-artifact-download="${escAttr(downloadUrl)}">Download</a>
        </div>`
      } else {
        html += `<a class="msg-artifact-chip" href="${escAttr(downloadHref)}" download="${escAttr(name)}" data-artifact-category="${escAttr(category)}" data-artifact-download="${escAttr(downloadUrl)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}" title="${escAttr(name)}">
          <span class="msg-file-chip__icon" aria-hidden="true">${esc(artifactCategoryLabel(category))}</span>
          <span class="msg-file-chip__name">${esc(name)}</span>
          <span class="msg-file-chip__meta">${esc(meta)}</span>
        </a>`
      }
    })
    closeGroup()
    html += '</div>'
    return html
  }

  /* ── append a single artifact during streaming (chat.js:7457-7467) ─────── */

  function appendArtifact(payload: Artifact | null | undefined): void {
    if (!payload) return
    diag('artifact.append.start', { name: payload.name, mime: payload.mime })
    deps.pushStreamArtifact(payload)
    const bubble = deps.ensureStreamBubble()
    deps.markVisibleStreamEvent('artifact')
    const body = bubble.querySelector('.msg-body')
    if (body) body.insertAdjacentHTML('beforeend', renderArtifacts([payload]))
    if (deps.getAutoScroll()) deps.scrollToBottom()
    diag('artifact.append.done', { name: payload.name, mime: payload.mime })
  }

  /* ── re-render all stream artifacts after a reconcile (chat.js:7469-7478) ─ */

  function renderStreamArtifacts(): void {
    const bubble = deps.getStreamBubble()
    if (!bubble) return
    const body = bubble.querySelector('.msg-body')
    if (!body) return
    body.querySelectorAll('.msg-artifacts').forEach((el) => el.remove())
    const artifacts = deps.getStreamArtifacts()
    if (artifacts.length > 0) {
      body.insertAdjacentHTML('beforeend', renderArtifacts(artifacts))
      if (deps.getAutoScroll()) deps.scrollToBottom()
    }
  }

  /* ── download an artifact via authenticated fetch (chat.js:7653-7679) ──── */

  async function downloadArtifact(artifact: Artifact): Promise<void> {
    let downloadUrl = artifactDownloadUrl(artifact)
    if (!downloadUrl) return
    const headers: Record<string, string> = {}
    const sessionKey = deps.getSessionKey() || ''
    const token = deps.getAuthToken() || ''
    if (token) headers['Authorization'] = `Bearer ${token}`
    if (sessionKey) headers['x-agentos-session-key'] = sessionKey
    downloadUrl = artifactAuthenticatedDownloadUrl(downloadUrl, { sessionKey, token })
    const response = await fetch(downloadUrl, {
      method: 'GET',
      headers,
      credentials: 'same-origin',
    })
    if (!response.ok) {
      toast(`Download failed: HTTP ${response.status}`, 'warn', 3500)
      return
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = artifact.name || 'artifact'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  return {
    appendArtifact,
    renderArtifacts,
    renderStreamArtifacts,
    downloadArtifact,
  }
}

export type ArtifactRenderer = ReturnType<typeof createArtifactRenderer>
