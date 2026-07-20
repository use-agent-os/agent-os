import { useEffect } from 'react'

export function StubView({ title }: { title: string }) {
  useEffect(() => {
    document.title = `${title} - AgentOS Control`
  }, [title])
  return (
    <div className="p-8">
      <h2 className="text-lg font-semibold">{title}</h2>
      <p className="text-sm text-muted-foreground">Migration pending (see parity matrix).</p>
    </div>
  )
}
