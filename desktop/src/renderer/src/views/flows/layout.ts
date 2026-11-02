/**
 * Re-flow of the nodes the engine placed (`auto_pos`).
 *
 * The engine lays out a flow an agent wrote by estimating node heights; the
 * real ones (a textarea, an image grid after a run) differ, so its columns
 * overlap. Once the canvas has measured them, each column of engine-placed
 * nodes is stacked by its measured heights with a fixed gap, in the order it
 * already has. A node the person placed (no `auto_pos`) is never moved; it
 * only pushes the engine-placed ones below it further down.
 */

export const REFLOW_GAP = 48

export interface LayoutItem {
  id: string
  x: number
  y: number
  /** Measured height; 0 while not measured yet. */
  height: number
  auto: boolean
}

/** Same column: the engine places a column's nodes at one x. */
function columnKey(x: number): number {
  return Math.round(x)
}

/**
 * New y for each engine-placed node that has to move; empty when nothing
 * does (or a node of an affected column is not measured yet).
 */
export function reflowColumns(
  items: readonly LayoutItem[],
  gap = REFLOW_GAP,
): Record<string, { x: number; y: number }> {
  const columns = new Map<number, LayoutItem[]>()
  for (const item of items) {
    const key = columnKey(item.x)
    const list = columns.get(key) ?? []
    list.push(item)
    columns.set(key, list)
  }
  const moves: Record<string, { x: number; y: number }> = {}
  for (const column of columns.values()) {
    if (!column.some((i) => i.auto)) continue
    if (column.some((i) => i.height <= 0)) continue
    // Order as on screen; ties keep the given (document) order.
    const ordered = column
      .map((item, index) => ({ item, index }))
      .sort((a, b) => a.item.y - b.item.y || a.index - b.index)
      .map(({ item }) => item)
    let cursor = ordered[0]!.y
    for (const item of ordered) {
      if (item.auto) {
        if (Math.abs(item.y - cursor) > 0.5) moves[item.id] = { x: item.x, y: Math.round(cursor) }
        cursor = cursor + item.height + gap
      } else {
        cursor = Math.max(cursor, item.y + item.height + gap)
      }
    }
  }
  return moves
}
