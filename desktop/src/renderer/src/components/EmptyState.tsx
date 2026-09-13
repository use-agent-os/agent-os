import type { LucideIcon } from 'lucide-react'

/** Centered empty state in the macOS "No Content" posture: glyph, title, one line of direction. */
export function EmptyState({
  icon: Icon,
  title,
  body,
}: {
  icon: LucideIcon
  title: string
  body: string
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
      <Icon className="mb-2 size-10 text-dim" strokeWidth={1.25} aria-hidden />
      <p className="text-[15px] font-semibold">{title}</p>
      <p className="max-w-xs text-muted-foreground">{body}</p>
    </div>
  )
}
