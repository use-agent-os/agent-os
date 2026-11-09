import { t } from '@/i18n'
import '@/i18n/en/webPalette'
import { comboParts } from '@/components/shortcut-combo'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/web/components/ui/dialog'
import { COMPOSER_BINDINGS, GLOBAL_BINDINGS, NAVIGATION_CHORDS, type Binding } from './bindings'

function Keys({ combo }: { combo: string }) {
  const steps = combo.split(' ')
  return (
    <span className="flex shrink-0 items-center gap-1">
      {steps.map((step, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 ? (
            <span className="text-small text-text-muted">{t('webPalette.keyThen')}</span>
          ) : null}
          {comboParts(step).map((part, j) => (
            <kbd
              key={j}
              className="min-w-6 rounded border border-border-control bg-surface-0 px-1.5 py-0.5 text-center text-small"
            >
              {part}
            </kbd>
          ))}
        </span>
      ))}
    </span>
  )
}

function Section({ title, bindings }: { title: string; bindings: readonly Binding[] }) {
  return (
    <section aria-label={title}>
      <h3 className="mb-1 text-small font-semibold tracking-wide text-text-muted uppercase">
        {title}
      </h3>
      <ul role="list" className="flex flex-col">
        {bindings.map((b) => (
          <li key={b.combo} className="flex items-center justify-between gap-4 py-1">
            <span>{t(b.descriptionKey)}</span>
            <Keys combo={b.combo} />
          </li>
        ))}
      </ul>
    </section>
  )
}

/** The web console's shortcut overview (command-palette spec "Shortcut overview"). */
export function ShortcutOverview({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-2xl">
        <DialogTitle>{t('webPalette.shortcutsTitle')}</DialogTitle>
        <DialogDescription className="sr-only">{t('webPalette.bindOverview')}</DialogDescription>
        <div className="grid gap-6 md:grid-cols-2">
          <div className="flex flex-col gap-6">
            <Section title={t('webPalette.categoryGlobal')} bindings={GLOBAL_BINDINGS} />
            <Section title={t('webPalette.categoryComposer')} bindings={COMPOSER_BINDINGS} />
          </div>
          <Section title={t('webPalette.categoryNavigation')} bindings={NAVIGATION_CHORDS} />
        </div>
      </DialogContent>
    </Dialog>
  )
}
