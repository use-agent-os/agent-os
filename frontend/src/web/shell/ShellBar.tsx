import type { CSSProperties, ReactNode } from 'react'

const TONE: Record<'accent' | 'info' | 'warn', string> = {
  accent: 'var(--accent)',
  info: 'var(--info)',
  warn: 'var(--warn)',
}

/**
 * A slim notice bar above the page (setup prompt, restart, protocol and
 * update notices): a tone-tinted wash fading off to the right, the icon in a
 * small tile, and an entrance that grows the bar's row (shell.css
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
          className={`flex min-h-11 items-center gap-3 py-1.5 pr-2 pl-3 max-sm:gap-2 ${className ?? ''}`}
        >
          <span
            aria-hidden
            className="grid size-6 shrink-0 place-items-center rounded-md bg-[color-mix(in_oklab,var(--bar-tone)_16%,transparent)] text-[color:var(--bar-tone)] [&_svg]:size-3.5"
          >
            {icon}
          </span>
          {children}
        </div>
      </div>
    </div>
  )
}
