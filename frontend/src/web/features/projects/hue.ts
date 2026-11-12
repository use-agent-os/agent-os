/** How many tints the `.pj-hue` palette in styles/page-projects.css has. */
export const HUE_COUNT = 6

/**
 * A stable palette slot for a name (a project, a channel): the same name
 * always gets the same tint, in both themes. FNV-1a over UTF-16 units.
 */
export function hueOf(name: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0) % HUE_COUNT
}

/** The initial a tile shows: the first letter or digit of the name. */
export function initialOf(name: string): string {
  const match = /[\p{L}\p{N}]/u.exec(name)
  return (match?.[0] ?? '#').toLocaleUpperCase()
}
