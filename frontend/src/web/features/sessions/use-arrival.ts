import { useEffect, useState } from 'react'

/**
 * True until shortly after `ready` first turns true: put a one-time entrance
 * class (`anim-stagger`) on a list while it is, so rows rise in when the data
 * first arrives and never again on a refetch, a sort or a ticking value.
 */
export function useArrival(ready: boolean, ms = 700): boolean {
  const [settled, setSettled] = useState(false)
  useEffect(() => {
    if (!ready || settled) return
    const id = window.setTimeout(() => setSettled(true), ms)
    return () => window.clearTimeout(id)
  }, [ready, settled, ms])
  return !settled
}

const NONE: ReadonlySet<string> = new Set()

/**
 * The keys that appeared after the list first settled: a row created or moved
 * in while the page is open animates in; the first load, a sort and a refetch
 * of the same rows do not. A key stays fresh long enough for its entrance.
 */
export function useFreshKeys(
  keys: readonly string[],
  settled: boolean,
  ms = 700,
): ReadonlySet<string> {
  const [known, setKnown] = useState<ReadonlySet<string> | null>(null)
  // Adjusting state while rendering: the first settled render records what
  // was already there, so none of it counts as new.
  if (settled && known === null) setKnown(new Set(keys))
  const added = settled && known ? keys.filter((k) => !known.has(k)) : []
  const signature = added.join('\n')
  useEffect(() => {
    if (!signature) return
    const id = window.setTimeout(
      () => setKnown((prev) => new Set([...(prev ?? []), ...signature.split('\n')])),
      ms,
    )
    return () => window.clearTimeout(id)
  }, [signature, ms])
  return added.length ? new Set(added) : NONE
}
