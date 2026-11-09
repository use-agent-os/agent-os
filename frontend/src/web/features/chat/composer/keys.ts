import type { SendShortcut } from '@/stores/preferences'

export interface ComposerKey {
  key: string
  shiftKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  altKey: boolean
  isComposing?: boolean
  keyCode?: number
}

/**
 * What an Enter press in the composer means (chat spec "Composer growth and
 * send shortcut", "Input-method and touch input"):
 *
 * - an Enter that confirms an IME composition never sends;
 * - on touch-first devices Return is a new line — the Send button sends;
 * - `enter` mode: Enter sends, Shift+Enter is a new line;
 * - `mod-enter` mode: Mod+Enter sends, Enter is a new line.
 */
export function composerEnterAction(
  e: ComposerKey,
  mode: SendShortcut,
  touch: boolean,
): 'send' | 'newline' | null {
  if (e.key !== 'Enter') return null
  if (e.isComposing || e.keyCode === 229) return null
  if (touch) return 'newline'
  const mod = e.metaKey || e.ctrlKey
  if (mode === 'mod-enter') return mod ? 'send' : 'newline'
  if (e.shiftKey || e.altKey) return 'newline'
  return mod || !e.shiftKey ? 'send' : 'newline'
}
