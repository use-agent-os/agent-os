import { useEffect } from 'react'

export function StubView({ title }: { title: string }) {
  useEffect(() => {
    document.title = `${title} - AgentOS Control`
  }, [title])
  return (
    <div className="px-7 py-6">
      <div className="t-label">Control · {title}</div>
      <h2 className="t-display mt-1.5">{title}</h2>
      <div className="mt-8 rounded-md border border-dashed border-border p-8 text-sm text-dim">
        Migration pending (see parity matrix).
      </div>
    </div>
  )
}
