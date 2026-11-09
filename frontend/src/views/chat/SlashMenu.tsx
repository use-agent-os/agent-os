import {
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react'
import { parseSlashInput, type SlashCommand } from './logic'
import { useShortcutDocs } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/chat'

// #137 — the menu consumes these from the composer's keydown (`handleKeyDown`
// below) while it is open, so they are documented, not dispatched.
const SLASH_SHORTCUTS = [
  { combo: 'arrowdown', description: 'Move to the next command' },
  { combo: 'arrowup', description: 'Move to the previous command' },
  { combo: 'enter', description: 'Run the highlighted command' },
  { combo: 'escape', description: 'Dismiss the menu for this input' },
] as const

/**
 * The slash-command menu (React).
 *
 * Ported from the imperative legacy menu:
 * the input parse `_handleSlashInput`, the menu render
 * `_renderSlashMenu`, the close `_closeSlashMenu`,
 * and select `_selectSlashCmd`. Unlike the legacy imperative DOM
 * this is idiomatic React: the composer value drives the open/filter state (via
 * the pure `parseSlashInput` + the legacy prefix filter), a real listbox renders
 * the filtered commands, and command execution is an injected callback
 * (`onExecute`) so the menu stays decoupled from the send/dispatch path.
 *
 * The catalog (`commands`) is loaded once by `useSlashCommands` (
 * `_loadSlashCommands`, RPC `commands.list_for_surface` / `{ surface: 'web_chat' }`)
 * and shared with the composer send path — the menu is a pure renderer over it.
 *
 * The `//` literal-slash escape is owned here: `parseSlashInput`
 * returns inactive for a `//`-prefixed value, so the menu never opens and the
 * composer's send path treats `//literal` as text (see ChatPage's send wiring).
 *
 * Keyboard nav is exposed via `handleRef` (an imperative handle) so the Composer
 * — which owns the textarea and its keydown — can consult the menu BEFORE its own
 * history/send handling: while the menu is open, ↑/↓ move the active item, Enter
 * executes it, and Escape closes it (arrow/enter, escape).
 */

/** The composer-facing keyboard handle. `handleKeyDown` returns true when the
 * menu consumed the key (the composer must then NOT run its own handling). */
export interface SlashMenuHandle {
  /** True when the menu is open (has at least one filtered command). */
  isOpen: () => boolean
  /**
   * Intercept a composer keydown while the menu is open. ArrowUp/Down move the
   * active item; Enter executes it; Escape closes. Returns true when consumed.
   */
  handleKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => boolean
}

export interface SlashMenuProps {
  /** The live composer value — drives open/filter. */
  value: string
  /** The normalized catalog, owned by useSlashCommands. */
  commands: SlashCommand[]
  /**
   * Execute a slash command's raw text.
   * ChatPage wires this to the send path so dispatch/RPC lives in one place.
   */
  onExecute: (text: string) => void
  /** Fired when the menu closes (Escape / after a select), so the composer can
   * clear its own open-state mirror if it tracks one. */
  onClose?: () => void
  /** The imperative keyboard handle for the composer to consult. */
  handleRef?: React.Ref<SlashMenuHandle>
  /** Stable listbox ID referenced by the composer textarea. */
  listboxId?: string
  /** Mirrors the active option ID back to the composer for aria-activedescendant. */
  onActiveDescendantChange?: (optionId: string | undefined) => void
}

export function SlashMenu({
  value,
  commands,
  onExecute,
  onClose,
  handleRef,
  listboxId,
  onActiveDescendantChange,
}: SlashMenuProps) {
  const generatedId = useId()
  const resolvedListboxId = listboxId ?? `chat-slash-${generatedId}`
  // Registered from the always-mounted component (it renders null when closed),
  // so the overlay lists these whether or not the menu happens to be open.
  useShortcutDocs('Slash commands', SLASH_SHORTCUTS)
  // The active index into the filtered list.
  const [activeIdx, setActiveIdx] = useState(0)
  // Escape dismisses the menu for the CURRENT input (sets
  // `_slashOpen = false` + empties the DOM). Reset when the input changes so
  // typing re-opens it — parity with `_handleSlashInput` re-rendering on input.
  const [dismissed, setDismissed] = useState(false)

  // Reset the active index to the top + clear any Escape-dismiss whenever the
  // input changes (sets `_slashIdx = 0` on each `_handleSlashInput`,
  // which also re-opens the menu when the input still matches). React's
  // "adjust state during render on a prop change" pattern (react.dev/learn/
  // you-might-not-need-an-effect) — track the previous value in STATE so the
  // reset runs during render, no setState-in-effect cascade.
  const [prevValue, setPrevValue] = useState(value)
  if (prevValue !== value) {
    setPrevValue(value)
    setActiveIdx(0)
    setDismissed(false)
  }

  // ── Filter the catalog for the current input ──────────
  // The pure parse decides active/query (and owns the `//` escape); the legacy
  // prefix filter narrows.
  const filtered = useMemo<SlashCommand[]>(() => {
    const parsed = parseSlashInput(value)
    if (!parsed.active) return []
    return commands.filter((c) => c.cmd.slice(1).startsWith(parsed.query))
  }, [value, commands])

  // the menu is "open" only when at least one command
  // matches AND it has not been Escape-dismissed for this input.
  const open = filtered.length > 0 && !dismissed

  // Keep the active index in range if the filtered list shrinks.
  const boundedIdx = filtered.length > 0 ? Math.min(activeIdx, filtered.length - 1) : 0
  const activeOptionId = open ? `${resolvedListboxId}-option-${boundedIdx}` : undefined

  useEffect(() => {
    onActiveDescendantChange?.(activeOptionId)
  }, [activeOptionId, onActiveDescendantChange])

  useEffect(
    () => () => {
      onActiveDescendantChange?.(undefined)
    },
    [onActiveDescendantChange],
  )

  const close = useCallback(() => {
    setDismissed(true)
    onClose?.()
  }, [onClose])

  // select → dispatch the command text for execution. Legacy
  // `_selectSlashCmd` closes the menu + clears the textarea then runs the action;
  // here execution (and the composer clear) is the injected `onExecute`'s job.
  const execute = useCallback(
    (cmd: SlashCommand | undefined) => {
      if (!cmd) return
      onClose?.()
      // `_executeSlashCommand` dispatches on the command text; the
      // command's own `cmd`/`name` is the canonical text (with no args from the
      // menu path — args only arrive via a typed send, ).
      onExecute(cmd.cmd || cmd.name || '')
    },
    [onClose, onExecute],
  )

  // ── The composer keyboard handle (arrow/enter, esc) ──
  // Snapshot the live open/filtered/index into a ref so the (stable) keyboard
  // handle reads the latest without capturing stale render values. Written in an
  // effect (never during render).
  const stateRef = useRef({ open, filtered, boundedIdx })
  useEffect(() => {
    stateRef.current = { open, filtered, boundedIdx }
  }, [open, filtered, boundedIdx])

  useImperativeHandle(
    handleRef,
    (): SlashMenuHandle => ({
      isOpen: () => stateRef.current.open,
      handleKeyDown: (e) => {
        const { open: isOpen, filtered: cmds, boundedIdx: idx } = stateRef.current
        if (!isOpen) return false
        if (e.key === 'ArrowDown') {
          e.preventDefault()
          setActiveIdx(Math.min(idx + 1, cmds.length - 1))
          return true
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault()
          setActiveIdx(Math.max(idx - 1, 0))
          return true
        }
        if (e.key === 'Enter') {
          e.preventDefault()
          execute(cmds[idx])
          return true
        }
        if (e.key === 'Escape') {
          e.preventDefault()
          close()
          return true
        }
        return false
      },
    }),
    [execute, close],
  )

  if (!open) return null

  return (
    <div
      id={resolvedListboxId}
      className="chat-slash"
      role="listbox"
      aria-label={t('chat.slashLandmark')}
    >
      {filtered.map((cmd, i) => (
        <div
          key={cmd.cmd || cmd.name || i}
          id={`${resolvedListboxId}-option-${i}`}
          role="option"
          aria-selected={i === boundedIdx}
          className={`chat-slash-item${i === boundedIdx ? ' chat-slash-item--active' : ''}`}
          // preventDefault on mousedown so the composer textarea does not blur
          // before the click selection runs; the click itself executes (legacy
          // click bind, ).
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => execute(cmd)}
        >
          <span className="chat-slash-cmd">{cmd.cmd}</span>
          <span className="chat-slash-desc">{cmd.desc}</span>
        </div>
      ))}
    </div>
  )
}
