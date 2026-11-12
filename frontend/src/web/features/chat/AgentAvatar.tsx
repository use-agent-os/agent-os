/** The agent's avatar: the AgentOS mark (the rail's) in a soft rounded tile. */
export function AgentAvatar({ size = 24 }: { size?: number }) {
  const mark = Math.round(size * 0.6)
  return (
    <span
      aria-hidden
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.3) }}
      className="inline-grid shrink-0 place-items-center border border-border bg-[linear-gradient(180deg,var(--surface-2),color-mix(in_oklab,var(--surface-1),var(--accent)_7%))] shadow-highlight"
    >
      <span className="shell-brand-mark" style={{ width: mark, height: mark }} />
    </span>
  )
}
