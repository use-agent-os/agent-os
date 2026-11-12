import type { CSSProperties, ReactNode } from 'react'

const TONE: Record<'accent' | 'info' | 'warn', string> = {
  accent: 'var(--accent)',
  info: 'var(--info)',
  warn: 'var(--warn)',
}

/**
 * A slim notice bar above the page (setup prompt, restart, protocol and
 * update notices): a faint tone wash fading off to the right, the icon in the
 * tone with a soft halo, and an entrance that grows the bar's row (shell.css
 * `.shell-bar`). Dismissal is instant: exits never outlast entrances, and
 * the notice is gone the moment it is dismissed.
 */
export function ShellBar({
  tone,
  icon,
  className,
  children,
}: {
  tone: keyof typeof TONE
  icon: ReactNode
  className?: string
  children: ReactNode
}) {
  return (
    <div
      role="status"
      data-tone={tone}
      className="shell-bar shrink-0"
      style={{ '--bar-tone': TONE[tone] } as CSSProperties}
    >
      <div>
        <div
          className={`flex min-h-10 items-center gap-2.5 py-1 pr-1.5 pl-4 text-[13px] max-sm:gap-2 max-sm:pl-3 ${className ?? ''}`}
        >
          <span aria-hidden className="shell-bar-icon">
            {icon}
          </span>
          {children}
        </div>
      </div>
    </div>
  )
}
