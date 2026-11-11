import { Brain, ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import { usePreferences } from '@/stores/preferences'
import type { Item } from '../model/types'
import { useChatActions } from './context'

type ThinkingItem = Extract<Item, { kind: 'thinkingRef' }>

/**
 * Model reasoning as a collapsed "Thinking" block (chat spec "Thinking
 * display"). Restored turns load the content with chat.thinking on first
 * expand; the show-thinking preference hides the block entirely.
 */
export function ThinkingBlock({ item, live }: { item: ThinkingItem; live?: boolean }) {
  const show = usePreferences((s) => s.showThinking)
  const rpc = useRpc()
  const { sessionKey } = useChatActions()
  const [open, setOpen] = useState(false)
  // Mounted on first expand, then kept so collapsing animates too.
  const [mounted, setMounted] = useState(false)
  const [loaded, setLoaded] = useState<string | null>(null)
  const [state, setState] = useState<'idle' | 'loading' | 'error'>('idle')
  const [error, setError] = useState('')
  if (!show) return null

  const text = item.text ?? loaded
  const toggle = () => {
    const next = !open
    setOpen(next)
    setMounted(true)
    if (next && text === null && item.messageId && state === 'idle') {
      setState('loading')
      rpc
        .call<{ reasoning?: string | null }>('chat.thinking', {
          sessionKey,
          messageId: item.messageId,
        })
        .then((res) => {
          setLoaded(res?.reasoning ?? '')
          setState('idle')
        })
        .catch((e: unknown) => {
          setError(e instanceof Error ? e.message : String(e))
          setState('error')
        })
    }
  }

  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={toggle}
        className="chat-step-row target text-ui text-text-muted"
      >
        <span className="chat-node" aria-hidden>
          <Brain />
        </span>
        <span className={cn('font-medium', live && 'chat-shimmer')}>
          {live ? t('webChat.thinkingLive') : t('webChat.thinking')}
        </span>
        <span className="flex-1" />
        <ChevronRight className="chat-chevron" aria-hidden />
      </button>
      {mounted ? (
        <div className="collapsible chat-collapse" data-open={open}>
          <div>
            <div className="chat-thought" aria-live="polite">
              {state === 'loading' ? (
                <span role="status" className="chat-shimmer">
                  {t('webChat.thinkingLoading')}
                </span>
              ) : state === 'error' ? (
                <span role="alert" className="text-danger">
                  {t('webChat.thinkingFailed', { message: error })}
                </span>
              ) : (
                text
              )}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
