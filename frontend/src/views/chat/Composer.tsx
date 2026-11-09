import { useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { ArrowUpIcon, PaperclipIcon, SlidersHorizontalIcon, SquareIcon, XIcon } from 'lucide-react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { toast } from 'sonner'
import { MAX_PENDING, sendButtonState, shouldAutofocusComposer } from './logic'
import { useShortcutDocs } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/chat'

// #137 — what the composer binds, in the order `onTextareaKeyDown` tests it, so
// the `?` overlay reports the composer accurately without the handler leaving
// the element it has to be bound to.
const COMPOSER_SHORTCUTS = [
  { combo: 'enter', description: 'Send the message' },
  { combo: 'tab', description: 'Take the suggested next prompt into an empty composer' },
  { combo: 'shift+enter', description: 'Insert a newline' },
  {
    combo: 'escape',
    description: 'Abort the turn, else recover the queue, else clear the composer',
  },
  { combo: 'alt+arrowup', description: 'Pop the most recent queued message back for editing' },
  { combo: 'alt+arrowdown', description: 'Queue the composer text behind the running turn' },
  { combo: 'arrowup', description: 'Walk back through sent history' },
  { combo: 'arrowdown', description: 'Walk forward through sent history' },
] as const

/**
 * The chat command line (React).
 *
 * Ported from the legacy imperative composer in static/js/views/chat.js: the
 * keydown bindings, the document-level ESC abort
 *the textarea auto-resize, sent-
 * message history cycling on ↑/↓, and autofocus
 *. Unlike the transcript region this is idiomatic React —
 * local state drives the value, and send/abort are injected callbacks so the
 * component stays decoupled from RPC. ChatPage wires `onSend` to the
 * useTranscript send action (`_onSend` → `chat.send`) and
 * `onAbort` to the abort action (`_onStop` → `chat.abort`).
 *
 * Task 9 (attachments): the composer is now attachment-aware. `onSend` still
 * receives the raw composer text — ChatPage normalizes it against the pending
 * buffer (`normalizeOutgoingComposerPayload`, ) and fires chat.send
 * with the attachments. The send-enable + no-op-guard here honor
 * legacy `hasPayload = text || _pendingAttachments.length > 0`:
 * an attachments-only send (empty text) is allowed. The pending-work guard
 * (— "Wait for file attachment processing to finish") blocks a send
 * while a read/upload is in flight.
 *
 * ChatPage also composes the enqueue-while-streaming branch and
 * slash-command handling, keeping the input component focused on
 * keyboard/input behavior rather than RPC ownership.
 */

const MIN_TEXTAREA_HEIGHT = 40 // fallback when minHeight is unset.
const MAX_TEXTAREA_HEIGHT = 160 // cap.

/** Imperative handle exposed via `composerRef` — clears / sets the textarea. */
export interface ComposerHandle {
  clear: () => void
  /** Move keyboard focus into the message field without changing its draft. */
  focus: () => void
  /**
   * Programmatically set the composer value + focus with the caret at the end
   * (— the pending-recover / drain-head write path). Resets the
   * ↑/↓ history cursor since the content is now user-editable text.
   */
  setValue: (text: string) => void
  /** The current composer text` reads). */
  getValue: () => string
}

export interface ComposerProps {
  /** Send the composed text. Wired to chat.send by ChatPage. */
  onSend: (text: string) => void
  /**
   * The live composer value, pushed up so ChatPage can drive the slash menu's
   * open/filter state (`_handleSlashInput` reads `_textarea.value`).
   * Called on every input change; the composer remains the value's owner.
   */
  onValueChange?: (value: string) => void
  /**
   * Slash-menu keyboard intercept . Consulted BEFORE the
   * composer's own history/send/ESC handling while the menu is open; returns true
   * when the menu consumed the key (the composer then does nothing further).
   */
  onSlashKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => boolean
  /**
   * Imperative handle so an out-of-band selection (a slash-menu mouse click,
   * `_textarea.value = ''`) can clear the composer's textarea.
   */
  composerRef?: React.Ref<ComposerHandle>
  /**
   * The rendered slash menu, mounted above the input row (inserts
   * the menu just above the composer). Null / absent when there is no menu.
   */
  slashMenu?: React.ReactNode
  /** ID of the slash-command listbox controlled by the message field. */
  slashListboxId?: string
  /** ID of the currently active slash-command option, when the listbox is open. */
  slashActiveDescendant?: string
  /** Abort the in-flight turn. Wired to chat.abort by ChatPage. */
  onAbort?: () => void
  /** Streaming in flight (legacy `_isStreaming`) — drives the Abort affordance. */
  busy: boolean
  /** Compaction in flight (legacy `_isCompactInFlightForCurrentSession`) — label only. */
  pendingCompaction?: boolean
  /**
   * The user's sent-message history, oldest→newest (legacy derives this from
   * `_messages` filtered to role 'user', ). Drives ↑/↓ cycling.
   */
  history?: string[]
  /**
   * Whether attachments are pending.
   * Enables an attachments-only send (empty text) and is passed to
   * `sendButtonState`. Default false (no attachments).
   */
  hasPendingAttachments?: boolean
  /**
   * Whether a read/upload is in flight.
   * When true, a send toasts "Wait for file attachment processing to finish" and
   * no-ops rather than sending a half-processed attachment.
   */
  hasPendingWork?: boolean
  /**
   * Attach files chosen via the composer's file picker (chat.js file input). The
   * drop/paste surfaces live on ChatPage; the picker is the composer-local one.
   */
  onAttachFiles?: (files: File[] | FileList) => void
  /**
   * Optional slot for the attachment tray, rendered above the input row so the
   * previews sit with the composer.
   */
  tray?: React.ReactNode
  /** Imperative router-fx mount point, rendered as a compact status above the input row. */
  routerFxDock?: React.ReactNode
  /**
   * Optional route picker, mounted at the right of the input bar next to the
   * send button so the active model route is readable without opening the
   * settings popover, and sits with the other per-send controls.
   */
  routePicker?: React.ReactNode
  /**
   * Optional composer-settings toolbar (execution mode + Pilot Router + usage),
   * mounted behind a gear trigger in the input bar (
   * `chat-toolbar-wrap`). Absent when the view has no toolbar.
   */
  toolbar?: React.ReactNode
  /**
   * The current pending-queue length (legacy `_pendingQueue.length`). Drives the
   * ESC priority chain + the Alt+↓ enqueue cap guard
   *. Default 0.
   */
  pendingCount?: number
  /**
   * ESC pending-recover rung. Called
   * when ESC is pressed while NOT streaming but the queue is non-empty. ChatPage
   * owns the queue → recovers it into the composer. Returns true if it recovered.
   */
  onRecoverPending?: () => boolean
  /** Alt+↑ — pop the most-recent pending item into the composer. */
  onPopPendingTail?: () => void
  /** Alt+↓ — enqueue the current composer text. */
  onEnqueueCurrent?: () => void
  /**
   * The suggested next prompt (`chat.suggestNext`). Shown as the placeholder
   * while the composer is empty; Tab takes it into the input, Enter sends it.
   */
  suggestion?: string | null
}

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
  toolbar,
  pendingCount = 0,
  onRecoverPending,
  onPopPendingTail,
  onEnqueueCurrent,
  suggestion = null,
}: ComposerProps) {
  const [value, setValue] = useState('')
  const [toolbarOpen, setToolbarOpen] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const toolbarWrapRef = useRef<HTMLDivElement>(null)
  const toolbarTriggerRef = useRef<HTMLButtonElement>(null)
  const toolbarCloseRef = useRef<HTMLButtonElement>(null)
  const reduceMotion = useReducedMotion()

  // The composer binds its own keys on the textarea (it needs them before the
  // document sees them, and the slash menu has to be able to consume them), so
  // these are documented rather than dispatched. Descriptions are written from
  // `onTextareaKeyDown` below — keep them in step with it.
  useShortcutDocs('Composer', COMPOSER_SHORTCUTS)

  // Close the composer-settings popover on an outside click / Escape (it
  // previously stayed open until the toolbar trigger was clicked again). Bound only
  // while open; a mousedown outside the toolbar wrap or an Escape key closes it.
  useEffect(() => {
    if (!toolbarOpen) return
    toolbarCloseRef.current?.focus()
    const onDocMouseDown = (e: MouseEvent) => {
      if (!toolbarWrapRef.current?.contains(e.target as Node)) setToolbarOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // The open popover owns Escape before the textarea's clear/abort chain.
        e.preventDefault()
        e.stopPropagation()
        setToolbarOpen(false)
        toolbarTriggerRef.current?.focus()
      }
    }
    // `mousedown` (not `click`) so it fires before the popover's own handlers
    // and doesn't race the trigger's toggle.
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('keydown', onKeyDown, true)
    }
  }, [toolbarOpen])

  // History-cycle cursor (legacy `_inputHistoryIdx` / `_inputHistoryDraft`,
  // ). `null` = not navigating; the draft holds the pre-nav text.
  const historyIdxRef = useRef<number | null>(null)
  const historyDraftRef = useRef('')

  // `_autoResizeTextarea` — grow to fit content between a min
  // and a 160px cap; an empty value clears the inline height entirely.
  const autoResize = useCallback(() => {
    const ta = textareaRef.current
    if (!ta) return
    if (!ta.value) {
      ta.style.height = ''
      return
    }
    const minHeight = Number.parseFloat(getComputedStyle(ta).minHeight) || MIN_TEXTAREA_HEIGHT
    ta.style.height = 'auto'
    ta.style.height = Math.max(minHeight, Math.min(ta.scrollHeight, MAX_TEXTAREA_HEIGHT)) + 'px'
  }, [])

  // `_setTextareaProgrammatic` — write value + move the caret
  // to the end WITHOUT resetting the history cursor (the input handler's reset
  // is gated on user typing, ). In React the reset is keyed off the
  // input event vs. programmatic writes, so we set state directly and resize.
  const setProgrammatic = useCallback(
    (text: string) => {
      setValue(text)
      const ta = textareaRef.current
      if (ta) {
        // Apply immediately so the caret/resize don't wait for the next render.
        ta.value = text
        try {
          ta.setSelectionRange(text.length, text.length)
        } catch {
          /* ignore (jsdom / detached) */
        }
      }
      // Programmatic writes (send-clear, ESC-clear, history cycling) also drive
      // the slash-menu mirror so it re-evaluates on the new value (
      // — `_handleSlashInput` runs on every `_textarea.value` change).
      onValueChange?.(text)
      autoResize()
    },
    [autoResize, onValueChange],
  )

  // Autofocus on mount when the environment warrants it.
  useEffect(() => {
    if (typeof window !== 'undefined' && shouldAutofocusComposer(window)) {
      textareaRef.current?.focus({ preventScroll: true })
    }
  }, [])

  // Out-of-band clear (— a slash-menu mouse click clears the input).
  useImperativeHandle(
    composerRef,
    (): ComposerHandle => ({
      clear: () => {
        setProgrammatic('')
        historyIdxRef.current = null
        historyDraftRef.current = ''
      },
      focus: () => textareaRef.current?.focus({ preventScroll: true }),
      // the pending-recover / drain write: set the value,
      // focus with the caret at the end, and reset the history cursor since the
      // content is now user-editable text.
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

  // `_cycleHistory`. dir < 0 = older, dir > 0 = newer.
  // Returns true when the cursor moved (so the caller can preventDefault).
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
    // block while a read/upload is in flight so a half-processed
    // attachment is never sent ("Wait for file attachment processing to finish").
    if (hasPendingWork) {
      toast.warning('Wait for file attachment processing to finish')
      return
    }
    // `hasPayload = text || _pendingAttachments.length > 0`;
    // an attachments-only send (empty text) is allowed. When busy, we STILL call
    // `onSend` — ChatPage's `_onSend` port owns the enqueue-while-streaming/
    // compacting decision, enqueuing rather than sending. The
    // composer no longer swallows the busy send; it just clears its input, since
    // either a send fired or the payload moved to the pending queue.
    if (!text && !hasPendingAttachments) return
    onSend(text)
    setProgrammatic('')
    historyIdxRef.current = null
    historyDraftRef.current = ''
  }, [value, hasPendingAttachments, hasPendingWork, onSend, setProgrammatic])

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // IME composition guard.
      if (e.nativeEvent.isComposing || e.keyCode === 229) return

      // Slash menu first (— arrow/enter/escape are owned by
      // the open menu). When it consumes the key the composer does nothing else.
      if (onSlashKeyDown?.(e)) return

      // Tab takes the suggested next prompt — only into an empty composer and
      // only bare Tab, so Shift+Tab and Tab past a draft still move focus.
      if (
        e.key === 'Tab' &&
        suggestion &&
        !textareaRef.current?.value &&
        !e.shiftKey &&
        !e.altKey &&
        !e.ctrlKey &&
        !e.metaKey
      ) {
        e.preventDefault()
        setProgrammatic(suggestion)
        historyIdxRef.current = null
        historyDraftRef.current = ''
        return
      }

      // ESC priority chain (doc-level textarea):
      // 1. streaming → abort the turn (`_onStop`; the
      // stop path itself recovers pending, so the recover rung is skipped).
      // 2. pending non-empty → recover the whole queue into the composer
      //.
      // 3. has text → clear the input.
      // (The from-anywhere variant of rungs 1–2 lives on ChatPage's document
      // keydown; this handles ESC while the composer itself is focused.)
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
          return
        }
        return
      }

      // Alt+↑ — pop the most-recent pending item into the composer for editing
      //. Only when the queue is non-empty.
      if (e.key === 'ArrowUp' && e.altKey && pendingCount > 0) {
        e.preventDefault()
        onPopPendingTail?.()
        return
      }

      // Alt+↓ — enqueue the current composer text. Only when
      // there is text and the queue is not at the cap (MAX_PENDING).
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

      // Plain ↑: walk backwards through sent history when the textarea is empty
      // (entering nav) OR already navigating.
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

      // Plain ↓: walk forward only when already navigating.
      if (e.key === 'ArrowDown' && !e.altKey && !e.shiftKey && historyIdxRef.current !== null) {
        if (cycleHistory(1)) {
          e.preventDefault()
          return
        }
      }

      // Enter to send (no shift) — Shift+Enter inserts a newline.
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        doSend()
      }
    },
    [
      busy,
      onAbort,
      suggestion,
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

  // user typing resets the history cursor + resizes. The
  // value is pushed up so the slash menu can
  // re-open/filter/close from the current input.
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

  // The suggestion stands in for the placeholder only over an empty composer.
  const showSuggestion = Boolean(suggestion) && !value
  const { disabled: sendDisabled, label: sendLabel } = sendButtonState(
    value,
    busy,
    pendingCompaction,
    hasPendingAttachments,
  )

  const fileInputRef = useRef<HTMLInputElement>(null)
  const onFilePicked = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files && e.target.files.length > 0) onAttachFiles?.(e.target.files)
      // Reset so re-picking the same file fires change again.
      e.target.value = ''
    },
    [onAttachFiles],
  )

  return (
    <div className="chat-composer-shell">
      {routerFxDock}
      {tray}
      {slashMenu}
      <div className={busy ? 'chat-composer chat-composer--busy' : 'chat-composer'}>
        {toolbar ? (
          <div className="chat-toolbar-wrap" ref={toolbarWrapRef}>
            <button
              ref={toolbarTriggerRef}
              type="button"
              className="btn-term chat-toolbar-trigger"
              aria-haspopup="dialog"
              aria-expanded={toolbarOpen}
              aria-controls="chat-toolbar-popover"
              aria-label={t('chat.runModes')}
              title={t('chat.runModesTitle')}
              onClick={() => setToolbarOpen((v) => !v)}
            >
              <SlidersHorizontalIcon aria-hidden="true" />
            </button>
            <AnimatePresence initial={false}>
              {toolbarOpen ? (
                <motion.div
                  id="chat-toolbar-popover"
                  className="chat-toolbar-popover"
                  role="dialog"
                  aria-labelledby="chat-toolbar-popover-title"
                  initial={reduceMotion ? false : { opacity: 0, scale: 0.985, y: 4 }}
                  animate={{ opacity: 1, scale: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.985, y: 2 }}
                  transition={
                    reduceMotion ? { duration: 0 } : { duration: 0.22, ease: [0.16, 1, 0.3, 1] }
                  }
                >
                  <div className="chat-toolbar-popover__header">
                    <h2 id="chat-toolbar-popover-title" className="chat-toolbar-popover__title">
                      {t('chat.runModes')}
                    </h2>
                    <button
                      ref={toolbarCloseRef}
                      type="button"
                      className="chat-toolbar-popover__close"
                      aria-label={t('chat.runModesClose')}
                      title={t('chat.runModesClose')}
                      onClick={() => {
                        setToolbarOpen(false)
                        toolbarTriggerRef.current?.focus()
                      }}
                    >
                      <XIcon aria-hidden="true" />
                    </button>
                  </div>
                  <div className="chat-toolbar-popover__body">{toolbar}</div>
                </motion.div>
              ) : null}
            </AnimatePresence>
          </div>
        ) : null}
        {onAttachFiles ? (
          <>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/png,image/jpeg,image/gif,image/webp,application/pdf,text/plain,text/markdown,text/html,text/csv,application/json,.png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.md,.markdown,.html,.htm,.csv,.json"
              className="chat-composer__file-input"
              onChange={onFilePicked}
              aria-label={t('chat.attachFiles')}
              hidden
            />
            <button
              type="button"
              className="btn-term chat-composer__attach"
              onClick={() => fileInputRef.current?.click()}
              title={t('chat.attachFileTitle')}
              aria-label={t('chat.attachFiles')}
            >
              <PaperclipIcon aria-hidden="true" />
            </button>
          </>
        ) : null}
        <textarea
          ref={textareaRef}
          className="chat-composer__input"
          value={value}
          onChange={onChange}
          onKeyDown={onKeyDown}
          placeholder={(showSuggestion && suggestion) || t('chat.messagePlaceholder')}
          aria-keyshortcuts={showSuggestion ? 'Tab' : undefined}
          rows={1}
          aria-label={t('chat.messageLabel')}
          aria-autocomplete={slashListboxId ? 'list' : undefined}
          aria-expanded={slashListboxId ? Boolean(slashActiveDescendant) : undefined}
          aria-controls={slashActiveDescendant ? slashListboxId : undefined}
          aria-activedescendant={slashActiveDescendant}
        />
        {showSuggestion ? (
          <kbd
            className="chat-composer__suggestion-key"
            data-testid="composer-suggestion-key"
            title={t('chat.suggestionKeyTitle')}
            aria-hidden="true"
          >
            {t('chat.suggestionKey')}
          </kbd>
        ) : null}
        {routePicker}
        {busy ? (
          <button
            type="button"
            className="btn-term chat-composer__abort"
            onClick={() => onAbort?.()}
            title={t('chat.stopTitle')}
            aria-label={t('chat.stop')}
          >
            <SquareIcon aria-hidden="true" />
          </button>
        ) : (
          <button
            type="button"
            className="btn-term chat-composer__send"
            onClick={doSend}
            disabled={sendDisabled}
            title={sendLabel}
            aria-label={t('chat.send')}
          >
            <ArrowUpIcon aria-hidden="true" />
          </button>
        )}
      </div>
    </div>
  )
}
