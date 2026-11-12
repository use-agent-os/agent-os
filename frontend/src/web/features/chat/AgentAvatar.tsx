import { cn } from '@/lib/utils'

export interface AgentAvatarProps {
  /** Edge of the tile in px (default 24). The mark scales with it. */
  size?: number
  className?: string
}

/**
 * The agent's identity: the AgentOS mark in a soft tile. Decorative — put the
 * agent's name in text next to it. Shared by the transcript (assistant turns)
 * and the chat stage (composer chip, hero).
 */
export function AgentAvatar({ size = 24, className }: AgentAvatarProps) {
  return (
    <span
      aria-hidden
      data-slot="agent-avatar"
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.3) }}
      className={cn(
        'inline-grid shrink-0 place-items-center border border-border shadow-highlight',
        'bg-[linear-gradient(180deg,var(--surface-2),color-mix(in_oklab,var(--surface-1),var(--accent)_7%))]',
        className,
      )}
    >
      <span
        className="shell-brand-mark"
        style={{ width: Math.round(size * 0.62), height: Math.round(size * 0.62) }}
      />
    </span>
  )
}
