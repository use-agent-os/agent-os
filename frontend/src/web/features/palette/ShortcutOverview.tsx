import { t } from '@/i18n'
import '@/i18n/en/webPalette'
import { comboParts } from '@/components/shortcut-combo'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/web/components/ui/dialog'
import { Kbd } from '@/web/components/ui/kbd'
import { SectionHeader } from '@/web/components/ui/section-header'
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
            <Kbd key={j} className="min-w-6">
              {part}
            </Kbd>
          ))}
        </span>
      ))}
    </span>
  )
}

function Section({ title, bindings }: { title: string; bindings: readonly Binding[] }) {
  return (
    <section aria-label={title}>
      <SectionHeader as="h3" size="sm" title={title} className="mb-2" />
      <ul
        role="list"
        className="flex flex-col rounded-card border border-border bg-surface-2/50 px-3 shadow-highlight"
      >
        {bindings.map((b) => (
          <li
            key={b.combo}
            className="flex items-center justify-between gap-4 border-b border-border py-2 last:border-b-0"
          >
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
      <DialogContent className="max-w-3xl">
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
