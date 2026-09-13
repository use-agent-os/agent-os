import { ArrowUp, Paperclip, Square } from 'lucide-react'
import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from 'react'
import { toast } from 'sonner'
import type { ComposerHandle } from '@/views/chat/Composer'
import { MAX_PENDING, sendButtonState } from '@/views/chat/logic'
import { t } from '~/i18n'

export type { ComposerHandle }

/**
 * Same contract as the web console's Composer (frontend/src/views/chat/
 * Composer.tsx) so the shared chat hooks drive it unchanged; the markup and
 * styling are the desktop's own. Keyboard behaviour is ported verbatim:
 * Enter sends, Shift+Enter newline, Escape aborts → recovers queue → clears,
 * Alt+↑/↓ pop/enqueue pending, ↑/↓ walk sent history.
 */
export interface ComposerProps {
  onSend: (text: string) => void
  onValueChange?: (value: string) => void
  onSlashKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => boolean
  composerRef?: Ref<ComposerHandle>
  slashMenu?: ReactNode
  slashListboxId?: string
  slashActiveDescendant?: string
  onAbort?: () => void
  busy: boolean
  pendingCompaction?: boolean
  history?: string[]
  hasPendingAttachments?: boolean
  hasPendingWork?: boolean
  onAttachFiles?: (files: File[] | FileList) => void
  tray?: ReactNode
  routerFxDock?: ReactNode
  routePicker?: ReactNode
  pendingCount?: number
  onRecoverPending?: () => boolean
  onPopPendingTail?: () => void
  onEnqueueCurrent?: () => void
  autoFocus?: boolean
  /**
   * Enter sends and Shift+Enter breaks the line (default). Off: Enter breaks
   * the line and ⌘Enter sends. Settings > General.
   */
  enterToSend?: boolean
}

const MIN_TEXTAREA_HEIGHT = 26
const MAX_TEXTAREA_HEIGHT = 160

export function Composer({
  onSend,
  onValueChange,
  onSlashKeyDown,
  slashMenu,
  slashListboxId,
  slashActiveDescendant,
  composerRef,
  onAbort,
  busy,
  pendingCompaction = false,
  history = [],
  hasPendingAttachments = false,
  hasPendingWork = false,
  onAttachFiles,
  tray,
  routerFxDock,
  routePicker,
  pendingCount = 0,
  onRecoverPending,
  onPopPendingTail,
  onEnqueueCurrent,
  autoFocus = true,
  enterToSend = true,
}: ComposerProps) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const historyIdxRef = useRef<number | null>(null)
  const historyDraftRef = useRef('')

  const autoResize = useCallback(() => {
    const ta = textareaRef.current
    if (!ta) return
    if (!ta.value) {
      ta.style.height = ''
      return
    }
    ta.style.height = 'auto'
    ta.style.height =
      Math.max(MIN_TEXTAREA_HEIGHT, Math.min(ta.scrollHeight, MAX_TEXTAREA_HEIGHT)) + 'px'
  }, [])

  const setProgrammatic = useCallback(
    (text: string) => {
      setValue(text)
      const ta = textareaRef.current
      if (ta) {
        ta.value = text
        try {
          ta.setSelectionRange(text.length, text.length)
        } catch {
          /* detached */
        }
      }
      onValueChange?.(text)
      autoResize()
    },
    [autoResize, onValueChange],
  )

  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus({ preventScroll: true })
  }, [autoFocus])

  useImperativeHandle(
    composerRef,
    (): ComposerHandle => ({
      clear: () => {
        setProgrammatic('')
        historyIdxRef.current = null
        historyDraftRef.current = ''
      },
      focus: () => textareaRef.current?.focus({ preventScroll: true }),
      setValue: (text: string) => {
        setProgrammatic(text)
        historyIdxRef.current = null
        historyDraftRef.current = ''
        textareaRef.current?.focus({ preventScroll: true })
      },
      getValue: () => textareaRef.current?.value ?? '',
    }),
    [setProgrammatic],
  )

  const cycleHistory = useCallback(
    (dir: number): boolean => {
      if (history.length === 0) return false
      if (dir < 0) {
        if (historyIdxRef.current === null) {
          historyDraftRef.current = textareaRef.current?.value ?? value ?? ''
          historyIdxRef.current = history.length - 1
        } else {
          historyIdxRef.current = Math.max(0, historyIdxRef.current - 1)
        }
        setProgrammatic(history[historyIdxRef.current] ?? '')
        return true
      }
      if (historyIdxRef.current === null) return false
      const next = historyIdxRef.current + 1
      if (next >= history.length) {
        historyIdxRef.current = null
        setProgrammatic(historyDraftRef.current)
        historyDraftRef.current = ''
      } else {
        historyIdxRef.current = next
        setProgrammatic(history[next] ?? '')
      }
      return true
    },
    [history, setProgrammatic, value],
  )

  const doSend = useCallback(() => {
    const text = value.trim()
    if (hasPendingWork) {
      toast.warning('Wait for file attachment processing to finish')
      return
    }
    if (!text && !hasPendingAttachments) return
    onSend(text)
    setProgrammatic('')
    historyIdxRef.current = null
    historyDraftRef.current = ''
  }, [value, hasPendingAttachments, hasPendingWork, onSend, setProgrammatic])

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.nativeEvent.isComposing || e.keyCode === 229) return
      if (onSlashKeyDown?.(e)) return
      if (e.key === 'Escape') {
        if (busy) {
          e.preventDefault()
          onAbort?.()
          return
        }
        if (pendingCount > 0) {
          e.preventDefault()
          onRecoverPending?.()
          return
        }
        if (textareaRef.current?.value) {
          e.preventDefault()
          setProgrammatic('')
          historyIdxRef.current = null
          historyDraftRef.current = ''
        }
        return
      }
      if (e.key === 'ArrowUp' && e.altKey && pendingCount > 0) {
        e.preventDefault()
        onPopPendingTail?.()
        return
      }
      if (
        e.key === 'ArrowDown' &&
        e.altKey &&
        textareaRef.current?.value &&
        pendingCount < MAX_PENDING
      ) {
        e.preventDefault()
        onEnqueueCurrent?.()
        return
      }
      if (
        e.key === 'ArrowUp' &&
        !e.altKey &&
        !e.shiftKey &&
        (!textareaRef.current?.value || historyIdxRef.current !== null)
      ) {
        if (cycleHistory(-1)) {
          e.preventDefault()
          return
        }
      }
      if (e.key === 'ArrowDown' && !e.altKey && !e.shiftKey && historyIdxRef.current !== null) {
        if (cycleHistory(1)) {
          e.preventDefault()
          return
        }
      }
      if (e.key === 'Enter') {
        const sends = enterToSend ? !e.shiftKey && !e.metaKey : e.metaKey
        if (sends) {
          e.preventDefault()
          doSend()
        }
      }
    },
    [
      enterToSend,
      busy,
      onAbort,
      cycleHistory,
      doSend,
      setProgrammatic,
      onSlashKeyDown,
      pendingCount,
      onRecoverPending,
      onPopPendingTail,
      onEnqueueCurrent,
    ],
  )

  const onChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setValue(e.target.value)
      historyIdxRef.current = null
      historyDraftRef.current = ''
      onValueChange?.(e.target.value)
      autoResize()
    },
    [autoResize, onValueChange],
  )

  const { disabled: sendDisabled, label: sendLabel } = sendButtonState(
    value,
    busy,
    pendingCompaction,
    hasPendingAttachments,
  )

  return (
    <div className="composer-shell">
      {routerFxDock}
      {tray}
      {slashMenu}
      <form
        className="composer"
        data-busy={busy}
        onSubmit={(e) => {
          e.preventDefault()
          doSend()
        }}
      >
        {onAttachFiles ? (
          <>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              accept="image/png,image/jpeg,image/gif,image/webp,application/pdf,text/plain,text/markdown,text/html,text/csv,application/json,.png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.md,.markdown,.html,.htm,.csv,.json"
              onChange={(e) => {
                if (e.target.files && e.target.files.length > 0) onAttachFiles(e.target.files)
                e.target.value = ''
              }}
              aria-label={t('composer.attach')}
            />
            <button
              type="button"
              className="composer-chip"
              aria-label={t('composer.attach')}
              title={t('composer.attach')}
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip className="size-4" strokeWidth={1.75} aria-hidden />
            </button>
          </>
        ) : null}
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={onChange}
          onKeyDown={onKeyDown}
          placeholder={t('composer.placeholder')}
          aria-label={t('composer.placeholder')}
          aria-autocomplete={slashListboxId ? 'list' : undefined}
          aria-expanded={slashListboxId ? Boolean(slashActiveDescendant) : undefined}
          aria-controls={slashActiveDescendant ? slashListboxId : undefined}
          aria-activedescendant={slashActiveDescendant}
        />
        {routePicker}
        {busy ? (
          <button
            type="button"
            className="composer-send"
            data-abort="true"
            onClick={() => onAbort?.()}
            aria-label={t('composer.stop')}
            title={t('composer.stop')}
          >
            <Square className="size-3.5 fill-current" strokeWidth={2} aria-hidden />
          </button>
        ) : (
          <button
            type="submit"
            className="composer-send"
            disabled={sendDisabled}
            aria-label={t('composer.send')}
            title={sendLabel}
          >
            <ArrowUp className="size-4" strokeWidth={2.5} aria-hidden />
          </button>
        )}
      </form>
    </div>
  )
}
