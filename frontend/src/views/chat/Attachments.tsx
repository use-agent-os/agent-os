import { useCallback, useRef, useState } from 'react'
import { toast } from 'sonner'
import { apiUrl } from '@/lib/api-origin'
import { authenticatedHeaders } from '@/lib/http-auth'
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
import { t } from '@/i18n'
import '@/i18n/en/chat'

/**
 * Chat attachments — the pending-attachment buffer + tray (React).
 *
 * Ported from the imperative legacy attachment surface in
 * static/js/views/chat.js: `_addAttachment`, the FileReader
 * inline path + staged `_uploadAttachmentStaged` ,
 * `_removeAttachmentByLocalId`, the outgoing-payload
 * normalization `_normalizeOutgoingComposerPayload`, and the
 * preview renderer `_renderAttachmentPreview`. Unlike the legacy
 * imperative DOM this is idiomatic React: `useAttachments` owns the buffer +
 * lifecycle, `<Attachments>` renders it.
 *
 * The staged upload (image/PDF > 2 MB) is NOT an RPC — it is a multipart POST to
 * the bridge endpoint `/api/v1/files/upload`, returning a
 * `{ file_uuid }` the outgoing payload references. The connection token (same
 * key useTranscript reads) is sent as `Authorization: Bearer …`.
 */

// the local-id counter (monotonic per view). A module-scope ref-
// equivalent held in the hook so ids stay unique across adds within a session.
let nextAttachmentIdSeed = 1

/** The public surface of `useAttachments`, threaded to `<Attachments>` + Composer. */
export interface UseAttachments {
  /** The live pending buffer. */
  attachments: PendingAttachment[]
  /** Stage files (drop / paste / file-picker) → `_addAttachment` per file. */
  addFiles: (files: File[] | FileList) => void
  /** Remove one entry by local id. */
  remove: (localId: number) => void
  /** Clear the whole buffer after a successful send. */
  clear: () => void
  /**
   * Replace the whole buffer (— the pending drain / recover
   * paths write `_pendingAttachments` directly). Used by the pending-queue
   * recover/drain to stack the queued attachments into the composer buffer.
   */
  setAll: (attachments: PendingAttachment[]) => void
  /** The inline cap/mime rejection to show in the tray , or null. */
  rejection: string | null
  /**
   * Normalize the outgoing payload for send. Merges the current
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
    // drop the entry, re-render (React handles the re-render).
    setAttachments((prev) => prev.filter((att) => att.local_id !== localId))
  }, [])

  const clear = useCallback(() => setAttachments([]), [])

  // replace the buffer wholesale (pending drain / recover).
  const setAll = useCallback((next: PendingAttachment[]) => setAttachments(next), [])

  // `_uploadAttachmentStaged` — multipart POST to the bridge.
  const defaultUpload = useCallback(
    async (file: File, mime: string): Promise<{ file_uuid: string }> => {
      const form = new FormData()
      // Re-wrap so the multipart part carries the resolved mime.
      const uploadFile =
        file.type === mime || typeof File !== 'function'
          ? file
          : new File([file], file.name, { type: mime })
      form.append('file', uploadFile, file.name)
      form.append('mime', mime)
      const url = apiUrl('/api/v1/files/upload')
      const response = await fetch(url, {
        method: 'POST',
        body: form,
        headers: authenticatedHeaders(url),
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

  // `_addAttachment`, ported per-file. Returns whether it staged.
  const addOne = useCallback(
    (file: File): boolean => {
      const mime = resolveAttachmentMime(file)
      if (!isAllowedAttachmentMime(mime)) {
        // unsupported mime, with the allowed-types label.
        const message = `Unsupported file: ${file.name || 'attachment'} (${mime}). Allowed: ${ATTACHMENT_ALLOWED_LABEL}`
        setRejection(message)
        toast.warning(message)
        return false
      }
      const hardCap = attachmentHardCapBytes(mime)
      if (file.size > hardCap) {
        // over the per-type hard cap.
        const message = `File too large: ${file.name || 'attachment'} (max ${Math.round(
          hardCap / 1024 / 1024,
        )} MB)`
        setRejection(message)
        toast.warning(message)
        return false
      }
      setRejection(null)
      const localId = nextLocalId()

      // ≤ 2 MB → inline base64-on-frame.
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

      // > 2 MB and not stageable (text-family) → reject.
      if (!canStageAttachmentMime(mime)) {
        const message = `File too large: ${file.name || 'attachment'} (text-family attachments are limited to ${Math.round(
          ATTACHMENT_TEXT_HARD_CAP_BYTES / 1000 / 1000,
        )} MB)`
        setRejection(message)
        toast.warning(message)
        return false
      }

      // > 2 MB image/PDF → staged upload.
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

  return { attachments, addFiles, remove, clear, setAll, rejection, normalizeForSend }
}

/* ── Presentational tray ─────────── */

export interface AttachmentsProps {
  api: UseAttachments
}

/**
 * The attachment tray — previews of the pending buffer + the inline cap/mime
 * rejection message. Hidden entirely when there is nothing pending and no
 * rejection (hides the preview when the buffer is empty). Image
 * entries render a thumbnail; everything else a file chip with a busy state
 * while reading/uploading.
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
                  {isBusy ? (
                    <span className="spinner attachment-chip__spinner" />
                  ) : (
                    t('chat.attachmentIconFallback')
                  )}
                </span>
                <span className="attachment-chip__name">{att.name}</span>
                <span className="attachment-chip__meta">{meta}</span>
                <button
                  type="button"
                  className="attachment-remove"
                  onClick={() => remove(att.local_id)}
                  title={t('chat.attachmentRemove')}
                  aria-label={t('chat.attachmentRemoveAria', { name: att.name })}
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
