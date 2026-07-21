import './chat.css'
import { useCallback, useEffect } from 'react'
import { useRpc } from '@/app/providers'
import { Attachments, useAttachments } from './Attachments'
import { Composer } from './Composer'
import { canonicalSessionKey, hasPendingAttachmentWork, readSessionFromUrl } from './logic'
import { useTranscript } from './useTranscript'

/**
 * Chat view — full-bleed shell.
 *
 * Mounts the scroll thread region (owned by the transcript controller) above a
 * pinned composer row. Task 9 adds the attachment surface: the pending buffer +
 * tray (`useAttachments` / `<Attachments>`), drag-and-drop + image-paste onto
 * the thread (chat.js:2543-2572), and the normalize-then-send path that threads
 * attachments into `chat.send` (chat.js:6078/6157).
 */
export function ChatPage() {
  // Read the RPC client so the provider seam is wired from the foundation
  // (parity chat.js:1200); consumed by useTranscript for history/stream/send.
  useRpc()

  // Resolve the initial session key from the URL (chat.js:1182-1187 →
  // canonicalized, chat.js:1159-1165), falling back to the stable webchat key.
  const sessionKey = canonicalSessionKey(
    readSessionFromUrl(typeof window !== 'undefined' ? window.location.search : '') ?? '',
  )

  const { containerRef, send, abort, busy, history } = useTranscript({ sessionKey })
  const attachments = useAttachments()

  useEffect(() => {
    document.title = 'Chat - AgentOS Control'
  }, [])

  // The composer's send: normalize the raw text against the pending buffer
  // (large-paste / page-dump → generated .txt), then fire chat.send with the
  // resulting attachments, and clear the buffer (chat.js:6078-6174).
  const onComposerSend = useCallback(
    async (text: string) => {
      // Not a slash command surface yet (Task 10) → allowSlashCommand:false.
      const normalized = await attachments.normalizeForSend(text, false)
      if (!normalized) return // over the text hard cap; the helper already toasted.
      send(normalized.text, normalized.attachments)
      attachments.clear()
    },
    [attachments, send],
  )

  // chat.js:2543-2555 — drag-and-drop files onto the thread stage the files.
  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      if (e.dataTransfer?.files?.length) attachments.addFiles(e.dataTransfer.files)
    },
    [attachments],
  )
  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
  }, [])

  // chat.js:2557-2571 — clipboard image paste stages the image(s) as attachments.
  const onPaste = useCallback(
    (e: React.ClipboardEvent) => {
      const items = e.clipboardData?.items
      if (!items) return
      const files: File[] = []
      for (let i = 0; i < items.length; i++) {
        const item = items[i]
        if (item && item.type.startsWith('image/')) {
          const file = item.getAsFile()
          if (file) files.push(file)
        }
      }
      if (files.length > 0) {
        attachments.addFiles(files)
        e.preventDefault()
      }
    },
    [attachments],
  )

  return (
    <div className="chat-stage" onDrop={onDrop} onDragOver={onDragOver} onPaste={onPaste}>
      <div className="chat-thread" ref={containerRef} />
      <Composer
        onSend={onComposerSend}
        onAbort={abort}
        busy={busy}
        history={history}
        hasPendingAttachments={attachments.attachments.length > 0}
        hasPendingWork={hasPendingAttachmentWork(attachments.attachments)}
        onAttachFiles={attachments.addFiles}
        tray={<Attachments api={attachments} />}
      />
    </div>
  )
}
