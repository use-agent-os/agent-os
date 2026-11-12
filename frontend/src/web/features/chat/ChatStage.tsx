import { useState, type ReactNode } from 'react'
import { isMac } from '@/components/shortcut-combo'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { cn } from '@/lib/utils'
import { usePreferences } from '@/stores/preferences'
import { Kbd } from '@/web/components/ui/kbd'
import '@/web/styles/chat-stage.css'

/** "Good morning" / "Good afternoon" / "Good evening" for the local hour. */
export function greetingFor(hour: number): string {
  if (hour >= 5 && hour < 12) return t('webChat.stageMorning')
  if (hour >= 12 && hour < 18) return t('webChat.stageAfternoon')
  return t('webChat.stageEvening')
}

/**
 * The living backdrop behind the stage: two slow aurora blobs and a dot grid
 * that fades towards the edges. Decorative: hidden from AT and in forced
 * colours, static under reduced motion.
 */
export function StageBackdrop({ className }: { className?: string }) {
  return (
    <div aria-hidden data-decorative className={cn('stage-backdrop', className)}>
      <span className="stage-aurora stage-aurora-a" />
      <span className="stage-aurora stage-aurora-b" />
    </div>
  )
}

/** The AgentOS mark in its lit tile, breathing gently. */
export function StageMark({ size = 'lg' }: { size?: 'md' | 'lg' }) {
  return (
    <span aria-hidden className="stage-mark" data-size={size}>
      <span className="shell-brand-mark" />
    </span>
  )
}

/** The display heading: the greeting (with a lime full stop) over the question. */
export function StageHeading({ as: Tag = 'h1', sub }: { as?: 'h1' | 'h2'; sub?: ReactNode }) {
  // Read once: the greeting does not flip while the page is open.
  const [greeting] = useState(() => greetingFor(new Date().getHours()))
  return (
    <div className="flex flex-col items-center gap-3 text-center">
      <Tag className="stage-title text-balance">
        <span className="stage-title-lead">
          {greeting}
          <span className="stage-title-dot">.</span>
        </span>{' '}
        <span className="stage-title-rest">{t('webChat.stageHeadline')}</span>
      </Tag>
      {sub ? <p className="max-w-md text-ui text-pretty text-text-muted">{sub}</p> : null}
    </div>
  )
}

/** Quiet keyboard hints under the stage: send, new line, the command palette. */
export function KeyHints({ className }: { className?: string }) {
  const sendShortcut = usePreferences((s) => s.sendShortcut)
  const mod = isMac() ? '⌘' : 'Ctrl'
  const send = sendShortcut === 'mod-enter' ? [mod, '⏎'] : ['⏎']
  const newline = sendShortcut === 'mod-enter' ? ['⏎'] : ['⇧', '⏎']
  const hints: Array<[string[], string]> = [
    [send, t('webChat.stageHintSend')],
    [newline, t('webChat.stageHintNewline')],
    [[mod, 'K'], t('webChat.stageHintCommands')],
  ]
  return (
    <ul aria-label={t('webChat.stageHints')} className={cn('stage-hints', className)}>
      {hints.map(([keys, label]) => (
        <li key={label} className="flex items-center gap-1.5">
          <span className="flex items-center gap-0.5">
            {keys.map((k) => (
              <Kbd key={k}>{k}</Kbd>
            ))}
          </span>
          {label}
        </li>
      ))}
    </ul>
  )
}

/**
 * An existing session with no messages: the new-session stage's language
 * (mark, display heading, hints) above the docked composer, so "new" and
 * "empty" read as one product.
 */
export function EmptyConversation() {
  return (
    <div className="stage-enter relative my-auto flex flex-col items-center gap-6 py-12 text-center">
      <StageBackdrop className="stage-backdrop-contained" />
      <StageMark />
      <StageHeading as="h2" sub={t('webChat.emptyBody')} />
      <KeyHints />
    </div>
  )
}
