import { useCallback, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  ATTACHMENT_ALLOWED_LABEL,
  ATTACHMENT_TEXT_HARD_CAP_BYTES,
  INLINE_THRESHOLD_BYTES,
  attachmentHardCapBytes,
  canStageAttachmentMime,
  isAllowedAttachmentMime,
  normalizeOutgoingComposerPayload,
  resolveAttachmentMime,
  type NormalizedComposerPayload,
  type PendingAttachment,
} from './logic'

/**
 * Chat attachments — the pending-attachment buffer + tray (React).
 *
 * Ported from the imperative legacy attachment surface in
 * static/js/views/chat.js: `_addAttachment` (chat.js:8052), the FileReader
 * inline path + staged `_uploadAttachmentStaged` (chat.js:8069/8127),
 * `_removeAttachmentByLocalId` (chat.js:8303), the outgoing-payload
 * normalization `_normalizeOutgoingComposerPayload` (chat.js:7982), and the
 * preview renderer `_renderAttachmentPreview` (chat.js:8346). Unlike the legacy
 * imperative DOM this is idiomatic React: `useAttachments` owns the buffer +
 * lifecycle, `<Attachments>` renders it.
 *
 * The staged upload (image/PDF > 2 MB) is NOT an RPC — it is a multipart POST to
 * the bridge endpoint `/api/v1/files/upload` (chat.js:8128-8144), returning a
 * `{ file_uuid }` the outgoing payload references. The connection token (same
 * key useTranscript reads) is sent as `Authorization: Bearer …`.
 */

// app.js:200-207 `getAuthToken` — the connection token in sessionStorage
// (providers.tsx / useTranscript use the same key), sent on the upload POST.
const WS_TOKEN_KEY = 'agentos.wsToken'
function getAuthToken(): string {
  try {
    return sessionStorage.getItem(WS_TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

// chat.js:322 — the local-id counter (monotonic per view). A module-scope ref-
// equivalent held in the hook so ids stay unique across adds within a session.
let nextAttachmentIdSeed = 1

/** The public surface of `useAttachments`, threaded to `<Attachments>` + Composer. */
export interface UseAttachments {
  /** The live pending buffer (chat.js:323 `_pendingAttachments`). */
  attachments: PendingAttachment[]
  /** Stage files (drop / paste / file-picker) → chat.js:8052 `_addAttachment` per file. */
  addFiles: (files: File[] | FileList) => void
  /** Remove one entry by local id (chat.js:8303 `_removeAttachmentByLocalId`). */
  remove: (localId: number) => void
  /** Clear the whole buffer after a successful send (chat.js:6173). */
  clear: () => void
  /** The inline cap/mime rejection to show in the tray (chat.js:8055/8059), or null. */
  rejection: string | null
  /**
   * Normalize the outgoing payload for send (chat.js:7982). Merges the current
   * pending buffer with any generated large-paste / page-dump attachment; returns
   * null (and toasts) when the paste exceeds the text hard cap.
   */
  normalizeForSend: (
    text: string,
    allowSlashCommand: boolean,
  ) => Promise<NormalizedComposerPayload | null>
}

/**
 * Own the pending-attachment buffer + its read/upload lifecycle. Optionally
 * inject `upload` (for tests); the default POSTs multipart to the bridge.
 */
export function useAttachments(opts?: {
  upload?: (file: File, mime: string) => Promise<{ file_uuid: string }>
}): UseAttachments {
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const [rejection, setRejection] = useState<string | null>(null)
  const nextIdRef = useRef(nextAttachmentIdSeed)
  const nextLocalId = useCallback(() => {
    const id = nextIdRef.current++
    nextAttachmentIdSeed = nextIdRef.current
    return id
  }, [])

  const remove = useCallback((localId: number) => {
    // chat.js:8303-8306 — drop the entry, re-render (React handles the re-render).
    setAttachments((prev) => prev.filter((att) => att.local_id !== localId))
  }, [])

  const clear = useCallback(() => setAttachments([]), [])

  // chat.js:8127-8161 `_uploadAttachmentStaged` — multipart POST to the bridge.
  const defaultUpload = useCallback(
    async (file: File, mime: string): Promise<{ file_uuid: string }> => {
      const form = new FormData()
      // Re-wrap so the multipart part carries the resolved mime (chat.js:8131-8133).
      const uploadFile =
        file.type === mime || typeof File !== 'function'
          ? file
          : new File([file], file.name, { type: mime })
      form.append('file', uploadFile, file.name)
      form.append('mime', mime)
      const headers: Record<string, string> = {}
      const token = getAuthToken()
      if (token) headers['Authorization'] = `Bearer ${token}`
      const response = await fetch('/api/v1/files/upload', {
        method: 'POST',
        body: form,
        headers,
        credentials: 'same-origin',
      })
      if (!response.ok) {
        const detail = await response.text().catch(() => '')
        throw new Error(`HTTP ${response.status} ${detail}`)
      }
      return (await response.json()) as { file_uuid: string }
    },
    [],
  )
  const upload = opts?.upload ?? defaultUpload

  // chat.js:8052-8125 `_addAttachment`, ported per-file. Returns whether it staged.
  const addOne = useCallback(
    (file: File): boolean => {
      const mime = resolveAttachmentMime(file)
      if (!isAllowedAttachmentMime(mime)) {
        // chat.js:8055 — unsupported mime, with the allowed-types label.
        const message = `Unsupported file: ${file.name || 'attachment'} (${mime}). Allowed: ${ATTACHMENT_ALLOWED_LABEL}`
        setRejection(message)
        toast.warning(message)
        return false
      }
      const hardCap = attachmentHardCapBytes(mime)
      if (file.size > hardCap) {
        // chat.js:8059 — over the per-type hard cap.
        const message = `File too large: ${file.name || 'attachment'} (max ${Math.round(
          hardCap / 1024 / 1024,
        )} MB)`
        setRejection(message)
        toast.warning(message)
        return false
      }
      setRejection(null)
      const localId = nextLocalId()

      // ≤ 2 MB → inline base64-on-frame (chat.js:8069).
      if (file.size <= INLINE_THRESHOLD_BYTES) {
        setAttachments((prev) => [
          ...prev,
          { kind: 'inline_pending', local_id: localId, name: file.name, mime, size: file.size },
        ])
        const reader = new FileReader()
        reader.onload = (e) => {
          const dataUrl = (e.target?.result as string) || ''
          const b64 = (dataUrl && dataUrl.split && dataUrl.split(',')[1]) || ''
          setAttachments((prev) => {
            const index = prev.findIndex((att) => att.local_id === localId)
            if (index < 0) return prev
            const next = prev.slice()
            next[index] = {
              kind: 'inline',
              local_id: localId,
              name: file.name,
              mime,
              size: file.size,
              data: b64,
              dataUrl,
            }
            return next
          })
        }
        reader.onerror = () => {
          remove(localId)
          toast.warning(`Could not read file: ${file.name || 'attachment'}`)
        }
        reader.readAsDataURL(file)
        return true
      }

      // > 2 MB and not stageable (text-family) → reject (chat.js:8103).
      if (!canStageAttachmentMime(mime)) {
        const message = `File too large: ${file.name || 'attachment'} (text-family attachments are limited to ${Math.round(
          ATTACHMENT_TEXT_HARD_CAP_BYTES / 1000 / 1000,
        )} MB)`
        setRejection(message)
        toast.warning(message)
        return false
      }

      // > 2 MB image/PDF → staged upload (chat.js:8112).
      setAttachments((prev) => [
        ...prev,
        { kind: 'uploading', local_id: localId, name: file.name, mime, size: file.size },
      ])
      upload(file, mime)
        .then((result) => {
          setAttachments((prev) => {
            const index = prev.findIndex((att) => att.local_id === localId)
            if (index < 0) return prev
            const next = prev.slice()
            next[index] = {
              kind: 'staged',
              local_id: localId,
              name: file.name,
              mime,
              size: file.size,
              file_uuid: result.file_uuid,
            }
            return next
          })
        })
        .catch((err: unknown) => {
          remove(localId)
          const detail = err instanceof Error ? err.message : String(err)
          toast.warning(`Upload failed for ${file.name || 'attachment'}: ${detail}`)
        })
      return true
    },
    [nextLocalId, remove, upload],
  )

  const addFiles = useCallback(
    (files: File[] | FileList) => {
      Array.from(files).forEach((file) => addOne(file))
    },
    [addOne],
  )

  const normalizeForSend = useCallback(
    (text: string, allowSlashCommand: boolean) =>
      normalizeOutgoingComposerPayload(text, attachments, {
        allowSlashCommand,
        nextLocalId,
        onToast: (message, level) => {
          if (level === 'warn') toast.warning(message)
          else toast.info(message)
        },
      }),
    [attachments, nextLocalId],
  )

  return { attachments, addFiles, remove, clear, rejection, normalizeForSend }
}

/* ── Presentational tray (chat.js:8346 `_renderAttachmentPreview`) ─────────── */

export interface AttachmentsProps {
  api: UseAttachments
}

/**
 * The attachment tray — previews of the pending buffer + the inline cap/mime
 * rejection message. Hidden entirely when there is nothing pending and no
 * rejection (chat.js:8348 hides the preview when the buffer is empty). Image
 * entries render a thumbnail; everything else a file chip with a busy state
 * while reading/uploading (chat.js:8355-8377).
 */
export function Attachments({ api }: AttachmentsProps) {
  const { attachments, remove, rejection } = api
  if (attachments.length === 0 && !rejection) return null
  return (
    <div className="chat-attachments">
      {rejection ? (
        <div className="chat-attachments__rejection tone-warn tone-rail" role="alert">
          {rejection}
        </div>
      ) : null}
      {attachments.length > 0 ? (
        <div className="chat-attachments__tray">
          {attachments.map((att) => {
            const isImage = (att.mime || '').startsWith('image/')
            const isBusy = att.kind === 'inline_pending' || att.kind === 'uploading'
            const status =
              att.kind === 'inline_pending'
                ? 'Reading...'
                : att.kind === 'uploading'
                  ? 'Uploading...'
                  : ''
            if (isImage && att.dataUrl) {
              return (
                <div className="attachment-thumb" key={att.local_id}>
                  <img src={att.dataUrl} alt={att.name} />
                  <button
                    type="button"
                    className="attachment-remove"
                    onClick={() => remove(att.local_id)}
                    aria-label={`Remove attachment ${att.name}`}
                  >
                    &times;
                  </button>
                  <span className="attachment-name">{att.name}</span>
                </div>
              )
            }
            const kb = att.size ? Math.max(1, Math.round(att.size / 1024)) + ' KB' : ''
            const stagedTag = att.kind === 'staged' ? ' • staged' : ''
            const meta = status || `${att.mime || ''} ${kb}${stagedTag}`.trim()
            return (
              <div
                className={`attachment-chip${isBusy ? ' attachment-chip--busy' : ''}`}
                data-mime={att.mime || ''}
                key={att.local_id}
              >
                <span className="attachment-chip__icon" aria-hidden="true">
                  {isBusy ? <span className="spinner attachment-chip__spinner" /> : 'file'}
                </span>
                <span className="attachment-chip__name">{att.name}</span>
                <span className="attachment-chip__meta">{meta}</span>
                <button
                  type="button"
                  className="attachment-remove"
                  onClick={() => remove(att.local_id)}
                  title="Remove"
                  aria-label={`Remove attachment ${att.name}`}
                >
                  &times;
                </button>
              </div>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
