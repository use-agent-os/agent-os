import type { Item, Turn } from '../model/types'

// The "Worked for …" fold (chat spec "Completed turns fold their work"). A
// selector over Turn.items: it groups for display and never changes data.

/** Kinds that count as the work before an answer. */
const WORK_KINDS = new Set<Item['kind']>(['thinkingRef', 'toolCall', 'text', 'steering'])

export interface FoldedTurn {
  /** Fold the work into one disclosure. */
  folded: boolean
  /** In original order: the folded work (empty when not folded). */
  work: Item[]
  /** Everything shown outside the disclosure, in original order. */
  visible: Item[]
  toolCount: number
  durationMs: number | null
}

export function foldTurn(turn: Turn): FoldedTurn {
  const items = turn.items.filter((i) => !(i.kind === 'toolCall' && i.name === 'router_control'))
  const toolCount = items.filter((i) => i.kind === 'toolCall').length
  const durationMs =
    turn.startedAt !== null && turn.endedAt !== null
      ? Math.max(0, turn.endedAt - turn.startedAt)
      : null
  // The answer: the last text after which no tool call follows (a failed tool
  // after it is a trailing failure, still shown outside the fold).
  let answer = -1
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]!
    if (item.kind === 'text' && item.text.trim()) {
      answer = i
      break
    }
    if (item.kind === 'toolCall' && item.state !== 'failed') break
  }
  const hasWork = items.slice(0, Math.max(0, answer)).some((i) => WORK_KINDS.has(i.kind))
  if (turn.status !== 'done' || answer < 0 || !hasWork) {
    return { folded: false, work: [], visible: items, toolCount, durationMs }
  }
  const work: Item[] = []
  const visible: Item[] = []
  items.forEach((item, i) => {
    if (i < answer && WORK_KINDS.has(item.kind)) work.push(item)
    else visible.push(item)
  })
  return { folded: true, work, visible, toolCount, durationMs }
}

/** "42s", "3m 05s", "1h 2m". */
export function formatWorkDuration(ms: number | null): string {
  if (ms === null) return ''
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}
