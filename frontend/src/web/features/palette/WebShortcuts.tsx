import { useNavigate } from 'react-router'
import { useKeyboardShortcut, useShortcutOverlay } from '@/components/KeyboardShortcuts'
import { t } from '@/i18n'
import '@/i18n/en/webPalette'
import { useShell } from '@/web/shell/shell-store'
import { usePhone } from '@/web/shell/use-media-query'
import { GLOBAL_BINDINGS, NAVIGATION_CHORDS, type Binding } from './bindings'

function categoryLabel(binding: Binding): string {
  if (binding.category === 'navigation') return t('webPalette.categoryNavigation')
  if (binding.category === 'composer') return t('webPalette.categoryComposer')
  return t('webPalette.categoryGlobal')
}

function Bind({
  binding,
  run,
  allowInInputs = false,
}: {
  binding: Binding
  run: (e: KeyboardEvent) => void
  allowInInputs?: boolean
}) {
  useKeyboardShortcut(
    {
      combo: binding.combo,
      description: t(binding.descriptionKey),
      category: categoryLabel(binding),
      allowInInputs,
      documentationOnly: binding.documentationOnly,
    },
    run,
  )
  return null
}

/**
 * Registers the web console's bindings through the kept provider. Modifier
 * shortcuts work from the composer; single keys and chords are suppressed
 * while editing or while a dialog is open (the provider's own guard).
 */
export function WebShortcuts() {
  const navigate = useNavigate()
  const overview = useShortcutOverlay()
  const phone = usePhone()
  const handlers: Record<string, (e: KeyboardEvent) => void> = {
    'mod+k': (e) => {
      e.preventDefault()
      useShell.getState().setPaletteOpen(true)
    },
    'mod+b': (e) => {
      e.preventDefault()
      const shell = useShell.getState()
      if (phone) shell.setDrawerOpen(!shell.drawerOpen)
      else shell.togglePane()
    },
    'mod+/': (e) => {
      e.preventDefault()
      overview.open()
    },
    'mod+shift+o': (e) => {
      e.preventDefault()
      const agent = useShell.getState().activeAgent
      navigate(agent ? `/new?agent=${encodeURIComponent(agent)}` : '/new')
    },
  }
  const noop = () => {}
  return (
    <>
      {GLOBAL_BINDINGS.map((binding) => (
        <Bind
          key={binding.combo}
          binding={binding}
          run={handlers[binding.combo] ?? noop}
          allowInInputs={binding.combo.startsWith('mod+')}
        />
      ))}
      {NAVIGATION_CHORDS.map((chord) => (
        <Bind
          key={chord.combo}
          binding={chord}
          run={(e) => {
            e.preventDefault()
            navigate(chord.path)
          }}
        />
      ))}
    </>
  )
}
