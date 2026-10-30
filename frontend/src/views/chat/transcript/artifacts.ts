// Chat transcript — artifact cards + publish rendering (imperative).
//
// This module is part of the reviewed imperative boundary of the
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

import { apiOrigin, urlBase } from '@/lib/api-origin'
import {
  TICKET_SRC_ATTR,
  TICKET_VARIANT_ATTR,
  THUMBNAIL_VARIANT,
  type TicketOptions,
} from '@/lib/artifact-tickets'
import { t, type MessageKey } from '@/i18n'
import '@/i18n/en/chat'

export { publishArtifactTargetName } from './tools'

import { isCardsArtifact } from './cards'
import { isChartArtifact } from './chart'
import { isDcaArtifact } from './dca'
import { isFlowRunArtifact } from './flowRunMime'
import { isLpArtifact } from './lp'
import { isMarketsArtifact } from './markets'
import { isTriggerArtifact } from './trigger'

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

// chat.js:7538-7549 — category: visual | audio | data | document | code | file,
// plus the AgentOS-native 'chart' (chart.ts), 'cards' (cards.ts), 'lp' (lp.ts),
// 'markets' (markets.ts), 'dca' (dca.ts), 'trigger' (trigger.ts) and 'flowrun' (flowRun.ts) categories, which have no legacy
// counterpart: they render inline rather than as a download chip.
// NOTE: image/* maps to 'visual' (NOT 'image' — the brief example was wrong).
export function artifactCategory(artifact: Artifact | null | undefined): string {
  if (isChartArtifact(artifact)) return 'chart'
  if (isCardsArtifact(artifact)) return 'cards'
  if (isLpArtifact(artifact)) return 'lp'
  if (isMarketsArtifact(artifact)) return 'markets'
  if (isDcaArtifact(artifact)) return 'dca'
  if (isTriggerArtifact(artifact)) return 'trigger'
  if (isFlowRunArtifact(artifact)) return 'flowrun'
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
    case 'chart':
      return 'chart'
    case 'cards':
      return 'cards'
    case 'lp':
      return 'lp'
    case 'markets':
      return 'markets'
    case 'dca':
      return 'dca'
    case 'trigger':
      return 'trigger'
    case 'flowrun':
      return 'flow'
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

/* ── Kind (AgentOS-native; no legacy counterpart) ───────────────────────── */

/**
 * What the file is, in the user's words. Finer than `artifactCategory`
 * (which only picks the card shape): a card tile and its subtitle want
 * "Spreadsheet", not "data", and a `.docx` and a `.pdf` want different glyphs.
 */
export type ArtifactKind =
  | 'spreadsheet'
  | 'document'
  | 'pdf'
  | 'presentation'
  | 'image'
  | 'audio'
  | 'archive'
  | 'code'
  | 'data'
  | 'text'
  | 'web'
  | 'file'

const KIND_BY_EXTENSION: Record<string, ArtifactKind> = {
  xlsx: 'spreadsheet',
  xlsm: 'spreadsheet',
  xls: 'spreadsheet',
  ods: 'spreadsheet',
  numbers: 'spreadsheet',
  csv: 'spreadsheet',
  tsv: 'spreadsheet',
  docx: 'document',
  doc: 'document',
  odt: 'document',
  rtf: 'document',
  pages: 'document',
  pdf: 'pdf',
  pptx: 'presentation',
  ppt: 'presentation',
  odp: 'presentation',
  key: 'presentation',
  zip: 'archive',
  tar: 'archive',
  gz: 'archive',
  tgz: 'archive',
  '7z': 'archive',
  rar: 'archive',
  json: 'data',
  jsonl: 'data',
  ndjson: 'data',
  xml: 'data',
  parquet: 'data',
  sqlite: 'data',
  db: 'data',
  ipynb: 'data',
  txt: 'text',
  md: 'text',
  markdown: 'text',
  log: 'text',
  html: 'web',
  htm: 'web',
  py: 'code',
  js: 'code',
  ts: 'code',
  tsx: 'code',
  jsx: 'code',
  sql: 'code',
  sh: 'code',
  go: 'code',
  rs: 'code',
  java: 'code',
  rb: 'code',
  css: 'code',
  yaml: 'code',
  yml: 'code',
  toml: 'code',
}

const KIND_BY_MIME: Record<string, ArtifactKind> = {
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'spreadsheet',
  'application/vnd.ms-excel': 'spreadsheet',
  'text/csv': 'spreadsheet',
  'text/tab-separated-values': 'spreadsheet',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'document',
  'application/msword': 'document',
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'presentation',
  'application/vnd.ms-powerpoint': 'presentation',
  'application/zip': 'archive',
  'application/gzip': 'archive',
  'application/x-tar': 'archive',
  'application/json': 'data',
  'application/ndjson': 'data',
  'application/x-ndjson': 'data',
  'application/xml': 'data',
  'text/xml': 'data',
  'text/plain': 'text',
  'text/markdown': 'text',
  'text/html': 'web',
}

const KIND_LABEL_KEYS: Record<ArtifactKind, MessageKey> = {
  spreadsheet: 'chat.artifactKindSpreadsheet',
  document: 'chat.artifactKindDocument',
  pdf: 'chat.artifactKindPdf',
  presentation: 'chat.artifactKindPresentation',
  image: 'chat.artifactKindImage',
  audio: 'chat.artifactKindAudio',
  archive: 'chat.artifactKindArchive',
  code: 'chat.artifactKindCode',
  data: 'chat.artifactKindData',
  text: 'chat.artifactKindText',
  web: 'chat.artifactKindWeb',
  file: 'chat.artifactKindFile',
}

export function artifactKind(artifact: Artifact | null | undefined): ArtifactKind {
  const mime = artifactMime(artifact)
  if (mime.startsWith('image/')) return 'image'
  if (mime.startsWith('audio/')) return 'audio'
  // Extension before mime: the authoring tools name their files precisely,
  // while a generic mime (octet-stream, the literal "artifact") says nothing.
  const ext = artifactExtension(artifactName(artifact))
  const byExtension = KIND_BY_EXTENSION[ext]
  if (byExtension) return byExtension
  const byMime = KIND_BY_MIME[mime]
  if (byMime) return byMime
  if (AUDIO_EXTENSIONS.includes(ext)) return 'audio'
  return 'file'
}

/** The kind, in the user's language ("Spreadsheet", "PDF"). */
export function artifactKindLabel(kind: ArtifactKind): string {
  return t(KIND_LABEL_KEYS[kind])
}

/** A size the way Finder says it: KB under a megabyte, then MB with one decimal. */
export function artifactSizeLabel(size: number | undefined): string {
  const bytes = Number(size)
  if (!Number.isFinite(bytes) || bytes <= 0) return ''
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * The card subtitle: "Spreadsheet · XLSX · 5 KB". Each part is dropped when
 * unknown, so a nameless artifact still reads "File" rather than "File · ",
 * and an extension that only repeats the kind ("PDF · PDF") is left out.
 */
export function artifactSummary(artifact: Artifact | null | undefined): string {
  const label = artifactKindLabel(artifactKind(artifact))
  const ext = artifactExtension(artifactName(artifact)).toUpperCase()
  return [label, ext === label.toUpperCase() ? '' : ext, artifactSizeLabel(artifact?.size)]
    .filter(Boolean)
    .join(' · ')
}

// chat.js:7561-7563 — image = the 'visual' category.
export function isImageArtifact(artifact: Artifact | null | undefined): boolean {
  return artifactCategory(artifact) === 'visual'
}

// chat.js:7565-7567 — audio = the 'audio' category.
export function isAudioArtifact(artifact: Artifact | null | undefined): boolean {
  return artifactCategory(artifact) === 'audio'
}

// The still formats the gateway makes a PNG preview of (`?variant=thumbnail`);
// an animated or vector image previews as itself.
const THUMBNAIL_MIMES = new Set(['image/png', 'image/jpeg', 'image/jpg', 'image/webp'])

/** Whether an image artifact's inline preview can load the ticket's thumbnail. */
export function artifactHasThumbnail(artifact: Artifact | null | undefined): boolean {
  return THUMBNAIL_MIMES.has(artifactMime(artifact))
}

/**
 * Serialize a resolved artifact URL for the host. On the gateway-served
 * console it stays relative (legacy parity: `/api/v1/artifacts/...`). Off
 * gateway — the desktop renderer, loaded from disk — a relative path would
 * resolve against `file://` (or the dev server), so the gateway origin the
 * URL was resolved against is kept: the chip's href, the image preview, the
 * audio source and the chart payload all have to reach the gateway.
 */
function gatewayHref(url: URL): string {
  return apiOrigin() ? url.href : url.pathname + url.search + url.hash
}

// chat.js:7480-7492 — the clean (session-key-stripped) download URL.
export function artifactDownloadUrl(artifact: Artifact | null | undefined): string {
  let raw = artifact && artifact.download_url ? String(artifact.download_url) : ''
  if (!raw && artifact && artifact.id) raw = `/api/v1/artifacts/${encodeURIComponent(artifact.id)}`
  if (!raw) return ''
  try {
    const url = new URL(raw, urlBase())
    url.searchParams.delete('sessionKey')
    url.searchParams.delete('session_key')
    return gatewayHref(url)
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
    const url = new URL(raw, urlBase())
    if (ctx.sessionKey) url.searchParams.set('sessionKey', ctx.sessionKey)
    if (ctx.token) url.searchParams.set('token', ctx.token)
    return gatewayHref(url)
  } catch {
    return raw
  }
}

// chat.js:7583-7593 — authenticated download href for a raw URL: append
// sessionKey + token. Same globals→ctx treatment as above.
export function artifactAuthenticatedDownloadUrl(raw: string, ctx: ArtifactUrlContext): string {
  if (!raw) return ''
  try {
    const url = new URL(raw, urlBase())
    if (ctx.sessionKey) url.searchParams.set('sessionKey', ctx.sessionKey)
    if (ctx.token) url.searchParams.set('token', ctx.token)
    return gatewayHref(url)
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
  /**
   * Draw any chart placeholders that just entered `container` (chart.ts
   * `mountCharts`). AgentOS-native — no legacy counterpart. Default: no-op, so
   * a controller that never composes a mounter simply renders inert cards.
   */
  mountCharts?: (container: HTMLElement) => void
  /**
   * Trade a gateway file URL for a ticketed one (lib/artifact-tickets.ts);
   * null when none can be had, and the legacy URL + header apply. When it is
   * composed in, image and audio sources render as `data-ticket-src` for the
   * `mountCharts` pass to fill, downloads go through a ticket, and no URL on
   * the page carries the auth token. Default: absent (legacy URLs).
   */
  resolveTicket?: (url: string, options?: TicketOptions) => Promise<string | null>
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
  const mountCharts = deps.mountCharts ?? ((): void => {})

  const urlContext = (): ArtifactUrlContext => ({
    sessionKey: deps.getSessionKey() || '',
    token: deps.getAuthToken() || '',
  })

  /* ── render artifact cards HTML (chat.js:7595-7651) ───────────────────── */

  function renderArtifacts(artifacts: Artifact[] | null | undefined): string {
    if (!Array.isArray(artifacts) || artifacts.length === 0) return ''
    let html = '<div class="msg-artifacts">'
    let openGroup = ''
    const ticketed = Boolean(deps.resolveTicket)
    const context = urlContext()
    const sessionKey = context.sessionKey
    // With tickets nothing on the page needs the token: media loads through a
    // ticket and every download click is intercepted. Keep it out of the DOM.
    const token = ticketed ? '' : context.token
    const closeGroup = (): void => {
      if (!openGroup) return
      html += '</div>'
      openGroup = ''
    }
    artifacts.forEach((artifact) => {
      const category = artifactCategory(artifact)
      const groupKind =
        category === 'visual'
          ? 'visual'
          : category === 'chart'
            ? 'chart'
            : category === 'cards'
              ? 'cards'
              : category === 'lp'
                ? 'lp'
                : category === 'markets'
                  ? 'markets'
                  : category === 'dca'
                    ? 'dca'
                    : category === 'trigger'
                      ? 'trigger'
                      : category === 'flowrun'
                        ? 'flowrun'
                        : 'file'
      if (groupKind !== openGroup) {
        closeGroup()
        html +=
          groupKind === 'visual'
            ? '<div class="msg-artifact-gallery">'
            : groupKind === 'chart'
              ? '<div class="msg-artifact-charts">'
              : groupKind === 'cards'
                ? '<div class="msg-artifact-cards-group">'
                : groupKind === 'lp'
                  ? '<div class="msg-artifact-lp-group">'
                  : groupKind === 'markets'
                    ? '<div class="msg-artifact-markets-group">'
                    : groupKind === 'dca'
                      ? '<div class="msg-artifact-dca-group">'
                      : groupKind === 'trigger'
                        ? '<div class="msg-artifact-trigger-group">'
                        : groupKind === 'flowrun'
                          ? '<div class="msg-artifact-flowrun-group">'
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
      if (category === 'chart') {
        // A mount placeholder, not a finished card: the chart mounter fetches
        // `data-chart-src` and draws into `__canvas`. Geometry is reserved in
        // CSS so the transcript does not jump when the chart lands.
        //
        // The download button exports the rendered chart as an image (PNG) via
        // chart.takeScreenshot(). `data-artifact-download` stays OFF both the
        // host and the button so delegated transcript download handling does
        // not intercept it or fetch the raw JSON artifact payload.
        const payloadUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<div class="msg-artifact-chart" data-chart-src="${escAttr(payloadUrl)}" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <div class="msg-artifact-chart__header">
            <span class="msg-artifact-chart__name">${esc(name)}</span>
            <button type="button" class="msg-artifact-chart__download" aria-label="${escAttr(t('chat.artifactDownload'))}">${escAttr(t('chat.artifactDownload'))}</button>
          </div>
          <div class="msg-artifact-chart__readout"></div>
          <div class="msg-artifact-chart__canvas"></div>
          <p class="msg-artifact-chart__status">${escAttr(t('chat.chartLoading'))}</p>
        </div>`
      } else if (category === 'cards') {
        // A mount placeholder, not a finished grid: the cards mounter fetches
        // `data-cards-src` and builds the cards into `__grid`. No download
        // affordance — the payload is an internal render format, not a file the
        // user asked for, so `data-artifact-download` stays off deliberately.
        const payloadUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<div class="msg-artifact-cards" data-cards-src="${escAttr(payloadUrl)}" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <div class="msg-artifact-cards__header">
            <span class="msg-artifact-cards__name">${esc(name)}</span>
          </div>
          <div class="msg-artifact-cards__grid"></div>
          <p class="msg-artifact-cards__overflow" hidden></p>
          <p class="msg-artifact-cards__status">${escAttr(t('chat.cardsLoading'))}</p>
        </div>`
      } else if (category === 'lp') {
        // A mount placeholder for a Uniswap V4 liquidity card (lp.ts,
        // docs/lp-cards.md): the LP mounter fetches `data-lp-src` and builds
        // the card into `__body`. Like cards, no download affordance.
        const payloadUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<div class="msg-artifact-lp" data-lp-src="${escAttr(payloadUrl)}" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <div class="msg-artifact-lp__body"></div>
          <p class="msg-artifact-lp__status">${escAttr(t('chat.lpLoading'))}</p>
        </div>`
      } else if (category === 'markets') {
        // A mount placeholder for a markets card (markets.ts,
        // docs/markets.md): the markets mounter fetches `data-markets-src` and
        // builds the card into `__body`. Like the LP card, no download
        // affordance.
        const payloadUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<div class="msg-artifact-markets" data-markets-src="${escAttr(payloadUrl)}" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <div class="msg-artifact-markets__body"></div>
          <p class="msg-artifact-markets__status">${escAttr(t('chat.marketsLoading'))}</p>
        </div>`
      } else if (category === 'dca') {
        // A mount placeholder for a DCA mandate card (dca.ts, docs/dca.md):
        // the DCA mounter fetches `data-dca-src` and builds the card into
        // `__body`. Like the LP card, no download affordance.
        const payloadUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<div class="msg-artifact-dca" data-dca-src="${escAttr(payloadUrl)}" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <div class="msg-artifact-dca__body"></div>
          <p class="msg-artifact-dca__status">${escAttr(t('chat.dcaLoading'))}</p>
        </div>`
      } else if (category === 'trigger') {
        // A mount placeholder for a price trigger card (trigger.ts,
        // docs/triggers.md): the trigger mounter fetches `data-trigger-src` and
        // builds the card into `__body`. Like the DCA card, no download
        // affordance.
        const payloadUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<div class="msg-artifact-trigger" data-trigger-src="${escAttr(payloadUrl)}" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <div class="msg-artifact-trigger__body"></div>
          <p class="msg-artifact-trigger__status">${escAttr(t('chat.triggerLoading'))}</p>
        </div>`
      } else if (category === 'flowrun') {
        // A mount placeholder for an Image Flow run card (flowRun.ts,
        // docs/flows.md): the flow-run mounter fetches `data-flowrun-src`,
        // draws the card into `__body` and re-reads the run live. No download
        // affordance; the saved images are their own artifacts.
        const payloadUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        html += `<div class="msg-artifact-flowrun" data-flowrun-src="${escAttr(payloadUrl)}" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <div class="msg-artifact-flowrun__body"></div>
          <p class="msg-artifact-flowrun__status">${escAttr(t('chat.flowRunLoading'))}</p>
        </div>`
      } else if (isImageArtifact(artifact)) {
        const previewUrl = artifactPreviewUrl(artifact || {}, { sessionKey, token })
        // A ticketed preview waits for the mount pass to fill its source; a
        // still image loads the gateway's PNG thumbnail, the file stays the
        // download.
        const previewSource = ticketed
          ? `${TICKET_SRC_ATTR}="${escAttr(previewUrl)}"${artifactHasThumbnail(artifact) ? ` ${TICKET_VARIANT_ATTR}="${THUMBNAIL_VARIANT}"` : ''}`
          : `src="${esc(previewUrl)}"`
        html += `<a class="msg-artifact-card msg-artifact-card--image" href="${escAttr(downloadHref)}" download="${escAttr(name)}" data-artifact-category="${escAttr(category)}" data-artifact-download="${escAttr(downloadUrl)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}" title="${escAttr(t('chat.artifactDownloadTitle', { name }))}">
          ${previewUrl ? `<img class="msg-artifact-preview" ${previewSource} alt="${esc(name)}" loading="lazy">` : '<span class="msg-artifact-preview msg-artifact-preview--empty" aria-hidden="true"></span>'}
          <span class="msg-artifact-card__body">
            <span class="msg-artifact-card__name">${esc(name)}</span>
            <span class="msg-artifact-card__meta">${esc(meta)}</span>
          </span>
          <span class="msg-artifact-card__action" aria-hidden="true">${escAttr(t('chat.artifactDownload'))}</span>
        </a>`
      } else if (isAudioArtifact(artifact)) {
        html += `<div class="msg-artifact-card msg-artifact-card--audio" data-artifact-category="${escAttr(category)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}">
          <audio class="msg-artifact-audio" controls preload="metadata" ${ticketed ? TICKET_SRC_ATTR : 'src'}="${escAttr(downloadHref)}"></audio>
          <span class="msg-artifact-card__body">
            <span class="msg-artifact-card__name">${esc(name)}</span>
            <span class="msg-artifact-card__meta">${esc(meta)}</span>
          </span>
          <a class="msg-artifact-card__action" href="${escAttr(downloadHref)}" download="${escAttr(name)}" data-artifact-download="${escAttr(downloadUrl)}">${escAttr(t('chat.artifactDownload'))}</a>
        </div>`
      } else {
        // `data-artifact-kind` / `-summary` / `-action` feed a host that draws
        // the chip as a card (the desktop's tile, subtitle and Download
        // button come from these); the console's own skin reads the spans.
        html += `<a class="msg-artifact-chip" href="${escAttr(downloadHref)}" download="${escAttr(name)}" data-artifact-category="${escAttr(category)}" data-artifact-kind="${escAttr(artifactKind(artifact))}" data-artifact-summary="${escAttr(artifactSummary(artifact))}" data-artifact-action="${escAttr(t('chat.artifactDownload'))}" data-artifact-download="${escAttr(downloadUrl)}" data-artifact-id="${escAttr(artifact?.id || '')}" data-artifact-name="${escAttr(name)}" title="${escAttr(t('chat.artifactDownloadTitle', { name }))}">
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
    if (body) {
      body.insertAdjacentHTML('beforeend', renderArtifacts([payload]))
      mountCharts(body as HTMLElement)
    }
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
      mountCharts(body as HTMLElement)
      if (deps.getAutoScroll()) deps.scrollToBottom()
    }
  }

  /* ── download an artifact via authenticated fetch (chat.js:7653-7679) ──── */

  // A same-document link: the only way a script hands the browser a download.
  function clickDownloadLink(href: string, name: string): void {
    const a = document.createElement('a')
    a.href = href
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
  }

  async function downloadArtifact(artifact: Artifact): Promise<void> {
    let downloadUrl = artifactDownloadUrl(artifact)
    if (!downloadUrl) return
    const name = artifact.name || 'artifact'
    const sessionKey = deps.getSessionKey() || ''
    let ticketUrl: string | null = null
    if (deps.resolveTicket) {
      try {
        ticketUrl = await deps.resolveTicket(downloadUrl, { sessionKey })
      } catch (err) {
        toast(`Download failed: ${err instanceof Error ? err.message : String(err)}`, 'warn', 3500)
        return
      }
    }
    // On the gateway-served console a ticket is same-origin and needs no
    // header, so the browser downloads it itself, straight to disk. Off gateway
    // it is cross-origin, where Chromium ignores `download` and navigates: it
    // takes the fetch below, without a header.
    if (ticketUrl && !apiOrigin()) {
      clickDownloadLink(ticketUrl, name)
      return
    }
    const headers: Record<string, string> = {}
    if (ticketUrl) {
      downloadUrl = ticketUrl
    } else {
      const token = deps.getAuthToken() || ''
      if (token) headers['Authorization'] = `Bearer ${token}`
      if (sessionKey) headers['x-agentos-session-key'] = sessionKey
      downloadUrl = artifactAuthenticatedDownloadUrl(downloadUrl, { sessionKey, token })
    }
    let response: Response
    try {
      response = await fetch(downloadUrl, {
        method: 'GET',
        headers,
        credentials: 'same-origin',
      })
    } catch (err) {
      // Off gateway this is a cross-origin request: an unreachable gateway or a
      // refused CORS preflight rejects instead of answering, and a silent
      // rejection is exactly the "nothing happens" the toast exists to avoid.
      toast(`Download failed: ${err instanceof Error ? err.message : String(err)}`, 'warn', 3500)
      return
    }
    if (!response.ok) {
      toast(`Download failed: HTTP ${response.status}`, 'warn', 3500)
      return
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    clickDownloadLink(url, name)
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
