// The one place that knows what keys the console binds (#137).
//
// Before this, key handling was ad-hoc `keydown` listeners in ChatPage,
// Composer, SlashMenu, SessionChip and AppShell. Nothing could enumerate them,
// so nothing could document them, and each new one re-litigated the same
// overlay / editable-target guards.
//
// Shape:
//   - components register a `ShortcutSpec` via `useKeyboardShortcut`;
//   - the provider owns the single document listener and both guards;
//   - the `?` overlay (loaded lazily, `ShortcutOverlay.tsx`) renders whatever is
//     registered, with platform-aware caps from `comboParts` — the only place
//     that maps a combo to a label;
//   - keys whose behaviour is bound to an element rather than the document
//     (composer, slash menu, session switcher) register `documentationOnly`, so
//     the overlay stays honest without moving the handler.
import React, {
  createContext,
  lazy,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react'
import { overlayDepth } from '@/components/overlay-layer'
import {
  eventCombos,
  HELP_COMBO,
  type RegisteredShortcut,
  type ShortcutSpec,
} from './shortcut-combo'

export {
  comboParts,
  eventCombos,
  formatCombo,
  HELP_COMBO,
  isMac,
  type ShortcutSpec,
} from './shortcut-combo'

const ShortcutOverlay = lazy(() => import('./ShortcutOverlay'))

type RegisterFn = (
  id: string,
  spec: ShortcutSpec,
  handler: (e: KeyboardEvent) => void,
) => () => void

// Two contexts, so registering a shortcut does not re-render every other
// registrant: `register` is stable for the life of the provider, while the list
// changes as components mount.
const RegisterContext = createContext<RegisterFn | null>(null)
const HelpContext = createContext<{
  isHelpOpen: boolean
  setHelpOpen: (open: boolean) => void
} | null>(null)

function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el) return false
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable === true
}

/**
 * Renders the shortcut overview in place of the built-in sheet. The web console
 * passes its own dialog; desktop keeps the default.
 */
export type ShortcutHelpRenderer = (props: {
  open: boolean
  shortcuts: RegisteredShortcut[]
  onClose: () => void
}) => React.ReactNode

export function KeyboardShortcutProvider({
  children,
  renderHelp,
}: {
  children: React.ReactNode
  renderHelp?: ShortcutHelpRenderer
}) {
  const [shortcuts, setShortcuts] = useState<RegisteredShortcut[]>([])
  const [isHelpOpen, setHelpOpenState] = useState(false)
  // Keep the sheet's chunk mounted after the first open so its exit animation
  // can play; before that, nothing of it is downloaded at all.
  const [overlayLoaded, setOverlayLoaded] = useState(false)

  const setHelpOpen = useCallback((open: boolean) => {
    if (open) setOverlayLoaded(true)
    setHelpOpenState(open)
  }, [])

  // The document listener binds once; it reads the live registry and the live
  // overlay state through refs so it never has to be torn down and re-bound.
  const shortcutsRef = useRef<RegisteredShortcut[]>([])
  const helpOpenRef = useRef(false)
  useEffect(() => {
    shortcutsRef.current = shortcuts
  }, [shortcuts])
  useEffect(() => {
    helpOpenRef.current = isHelpOpen
  }, [isHelpOpen])

  // Keep track of pending prefix chords (e.g. 'g')
  const pendingPrefixRef = useRef<string | null>(null)
  const timeoutRef = useRef<NodeJS.Timeout | null>(null)

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current)
    }
  }, [])

  const register = useCallback<RegisterFn>((id, spec, handler) => {
    setShortcuts((prev) => [...prev, { id, spec, handler }])
    return () => setShortcuts((prev) => prev.filter((item) => item.id !== id))
  }, [])

  useEffect(() => {
    const clearPendingPrefix = () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
        timeoutRef.current = null
      }
      pendingPrefixRef.current = null
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return
      const combos = eventCombos(e)
      if (combos.length === 0) return

      const editable = isEditableTarget(e.target)
      const overlaid = overlayDepth() > 0

      // The sheet owns '?' in both directions. Closing has to be checked first.
      if (combos.includes(HELP_COMBO)) {
        clearPendingPrefix()
        if (helpOpenRef.current) {
          e.preventDefault()
          setHelpOpen(false)
          return
        }
        if (!editable && !overlaid) {
          e.preventDefault()
          setHelpOpen(true)
          return
        }
      }

      // Modifier or Escape cancels a pending prefix
      const hasModifier = e.ctrlKey || e.metaKey || e.altKey || e.shiftKey
      if (hasModifier || combos.includes('escape')) {
        clearPendingPrefix()
      }

      let matches: RegisteredShortcut[] = []
      const currentPrefix = pendingPrefixRef.current

      if (currentPrefix && !hasModifier && !combos.includes('escape')) {
        if (combos.includes(currentPrefix)) {
          if (timeoutRef.current) clearTimeout(timeoutRef.current)
          timeoutRef.current = setTimeout(() => {
            pendingPrefixRef.current = null
          }, 1500)
          return
        }
        const chordCombos = combos.map((c) => `${currentPrefix} ${c}`)
        matches = shortcutsRef.current.filter(
          (item) => !item.spec.documentationOnly && chordCombos.includes(item.spec.combo),
        )
        clearPendingPrefix()
      } else if (!currentPrefix && !hasModifier) {
        // Collect all potential prefixes dynamically
        const prefixes = new Set<string>()
        for (const s of shortcutsRef.current) {
          const parts = s.spec.combo.split(' ')
          if (parts.length > 1) {
            prefixes.add(parts[0]!)
          }
        }

        const matchedPrefix = combos.find((c) => prefixes.has(c))
        if (matchedPrefix && !editable && !overlaid) {
          pendingPrefixRef.current = matchedPrefix
          if (timeoutRef.current) clearTimeout(timeoutRef.current)
          timeoutRef.current = setTimeout(() => {
            pendingPrefixRef.current = null
          }, 1500)
          return
        }

        matches = shortcutsRef.current.filter(
          (item) => !item.spec.documentationOnly && combos.includes(item.spec.combo),
        )
      } else {
        matches = shortcutsRef.current.filter(
          (item) => !item.spec.documentationOnly && combos.includes(item.spec.combo),
        )
      }

      // Most-recently registered first, so a later mount can claim a key the
      // page below it also binds.
      for (let i = matches.length - 1; i >= 0; i -= 1) {
        const match = matches[i]!
        if (overlaid && !match.spec.allowWithOverlays) continue
        if (editable && !match.spec.allowInInputs) continue
        match.handler(e)
        if (e.defaultPrevented) break
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
    }
  }, [setHelpOpen])

  const help = useMemo(() => ({ isHelpOpen, setHelpOpen }), [isHelpOpen, setHelpOpen])
  const closeHelp = useCallback(() => setHelpOpen(false), [setHelpOpen])

  return (
    <RegisterContext.Provider value={register}>
      <HelpContext.Provider value={help}>
        {children}
        {renderHelp ? (
          renderHelp({ open: isHelpOpen, shortcuts, onClose: closeHelp })
        ) : overlayLoaded ? (
          <Suspense fallback={null}>
            <ShortcutOverlay open={isHelpOpen} shortcuts={shortcuts} onClose={closeHelp} />
          </Suspense>
        ) : null}
      </HelpContext.Provider>
    </RegisterContext.Provider>
  )
}

/**
 * Register a shortcut for as long as the component is mounted.
 *
 * Outside a provider this is a no-op rather than a throw: views are rendered
 * bare in unit tests, and a missing cheat-sheet must not break them.
 */
export function useKeyboardShortcut(spec: ShortcutSpec, handler: (e: KeyboardEvent) => void): void {
  const register = useContext(RegisterContext)
  const id = useId()

  // The handler is re-created on most renders; keep the registration stable and
  // read the latest through a ref, so the registry churns only on mount.
  const handlerRef = useRef(handler)
  useEffect(() => {
    handlerRef.current = handler
  }, [handler])

  const { combo, description, category, allowInInputs, allowWithOverlays, documentationOnly } = spec

  useEffect(() => {
    if (!register) return
    return register(
      id,
      { combo, description, category, allowInInputs, allowWithOverlays, documentationOnly },
      (e) => handlerRef.current(e),
    )
  }, [
    register,
    id,
    combo,
    description,
    category,
    allowInInputs,
    allowWithOverlays,
    documentationOnly,
  ])
}

const NOOP = () => {}

/**
 * Register a fixed list of documentation-only entries in one effect.
 *
 * For UI that binds its keys to its own element — the composer, the slash menu,
 * the session switcher — where the handler cannot move to the document but the
 * overlay should still report the key.
 *
 * Keyed on the entries' *content*, not their identity: registering re-renders
 * the caller, so depending on the array reference would make an inline literal
 * re-register forever.
 */
export function useShortcutDocs(
  category: string,
  entries: ReadonlyArray<{ combo: string; description: string }>,
): void {
  const register = useContext(RegisterContext)
  const id = useId()

  const signature = entries.map((entry) => `${entry.combo} ${entry.description}`).join('')
  const entriesRef = useRef(entries)
  // Effects run in declaration order within a commit, so this lands before
  // the registration below reads it.
  useEffect(() => {
    entriesRef.current = entries
  })

  useEffect(() => {
    if (!register) return
    const unregister = entriesRef.current.map((entry, index) =>
      register(
        `${id}-${index}`,
        {
          combo: entry.combo,
          description: entry.description,
          category,
          documentationOnly: true,
        },
        NOOP,
      ),
    )
    return () => unregister.forEach((fn) => fn())
  }, [register, id, category, signature])
}

/**
 * Open/close the sheet from the UI. `available` is false outside a provider, so
 * chrome can hide its affordance instead of offering a dead button.
 */
export function useShortcutOverlay(): {
  available: boolean
  isOpen: boolean
  open: () => void
  close: () => void
} {
  const ctx = useContext(HelpContext)
  const setHelpOpen = ctx?.setHelpOpen
  const open = useCallback(() => setHelpOpen?.(true), [setHelpOpen])
  const close = useCallback(() => setHelpOpen?.(false), [setHelpOpen])
  return { available: ctx != null, isOpen: ctx?.isHelpOpen ?? false, open, close }
}
