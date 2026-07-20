import './chat.css'
import { useEffect } from 'react'
import { useRpc } from '@/app/providers'
import { canonicalSessionKey, readSessionFromUrl } from './logic'
import { useTranscript } from './useTranscript'

/**
 * Chat view — full-bleed shell (Task 1 foundation).
 *
 * This is scaffolding: it mounts the scroll thread region (owned by the
 * transcript controller) above a pinned composer row, and sets the document
 * title. Nothing streams yet — RPC subscription, rendering, and the real
 * composer arrive in later tasks. `useRpc()` is read here so the seam matches
 * the migrated views (chat.js:1200 `App.getRpc()`), even though the client is
 * unused at this stage.
 */
export function ChatPage() {
  // Read the RPC client so the provider seam is wired from the foundation
  // (parity chat.js:1200); later tasks consume it for history/stream/send.
  useRpc()

  // Resolve the initial session key from the URL (chat.js:1182-1187 →
  // canonicalized, chat.js:1159-1165), falling back to the stable webchat key.
  const sessionKey = canonicalSessionKey(
    readSessionFromUrl(typeof window !== 'undefined' ? window.location.search : '') ?? '',
  )

  const { containerRef } = useTranscript({ sessionKey })

  useEffect(() => {
    document.title = 'Chat - AgentOS Control'
  }, [])

  return (
    <div className="chat-stage">
      <div className="chat-thread" ref={containerRef} />
      {/* Placeholder composer row — pinned; a later task fills it with the
          real command-line composer. */}
      <div className="chat-composer" />
    </div>
  )
}
