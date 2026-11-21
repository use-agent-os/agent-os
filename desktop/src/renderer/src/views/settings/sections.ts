/**
 * The settings rail. Two facts per section: its id and the
 * group it sits under. Copy lives in i18n (`settings.section.<id>`).
 */
export const SETTINGS_SECTIONS = [
  'overview',
  'providers',
  'router',
  'memory',
  'capabilities',
  'skills',
  'channels',
  'environment',
  'trading',
  'gateway',
  'appearance',
  'notifications',
  'security',
  'behaviour',
  'shortcuts',
  'advanced',
  'about',
] as const
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]

export const SETTINGS_GROUPS: readonly {
  /** `start` has no heading: it is the page Settings opens on. */
  id: 'start' | 'agent' | 'app' | 'more'
  sections: SettingsSection[]
}[] = [
  { id: 'start', sections: ['overview'] },
  {
    id: 'agent',
    sections: [
      'providers',
      'router',
      'memory',
      'capabilities',
      'skills',
      'channels',
      'environment',
      'trading',
    ],
  },
  {
    id: 'app',
    sections: ['gateway', 'appearance', 'notifications', 'security', 'behaviour', 'shortcuts'],
  },
  { id: 'more', sections: ['advanced', 'about'] },
]

export const DEFAULT_SECTION: SettingsSection = 'overview'

export function isSettingsSection(value: unknown): value is SettingsSection {
  return typeof value === 'string' && (SETTINGS_SECTIONS as readonly string[]).includes(value)
}

/** Lower-cased words; dots stay inside a word (`.env`, `config.toml`, `z.ai`). */
export function searchWords(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^\p{L}\p{N}.]+/u)
    .map((w) => w.replace(/\.+$/, ''))
    .filter(Boolean)
}

/** Index of the first word in `words` that `prefix` starts, or -1. */
function prefixAt(words: readonly string[], prefix: string): number {
  return words.findIndex((w) => w.startsWith(prefix))
}

/** Whether `query` appears in `words` as a run: "text size", "dark mode". */
function phraseAt(words: readonly string[], query: readonly string[]): number {
  for (let i = 0; i + query.length <= words.length; i++) {
    if (query.every((q, j) => words[i + j]!.startsWith(q))) return i
  }
  return -1
}

/**
 * How well a section answers the query, lower is better, or null when it
 * does not match. Every query word must start a word of the name or the
 * keywords, in any order. Then: the name begins with the query, every word
 * is in the name, the query is a phrase of the keywords, some word is in the
 * name, the words are scattered over the keywords. Within a rank, the
 * earlier in its list the keyword sits, the better (as a share of the list,
 * so long lists are not penalised): keywords are listed most telling first.
 */
export function sectionScore(
  query: string,
  text: { name: string; keywords: string },
): [rank: number, at: number] | null {
  const words = searchWords(query)
  if (words.length === 0) return [0, 0]
  const name = searchWords(text.name)
  const keywords = searchWords(text.keywords)
  const inName = words.map((w) => prefixAt(name, w) >= 0)
  const at = words.map((w, i) => (inName[i] ? -1 : prefixAt(keywords, w)))
  if (at.some((a, i) => a < 0 && !inName[i])) return null
  const share = (i: number) => i / Math.max(1, keywords.length)
  const where = share(Math.max(0, ...at))
  if (phraseAt(name, words) === 0) return [0, 0]
  if (inName.every(Boolean)) return [1, 0]
  const phrase = phraseAt(keywords, words)
  if (phrase >= 0) return [2, share(phrase)]
  if (inName.some(Boolean)) return [3, where]
  return [4, where]
}

/**
 * Rail search over the names and keywords (`settings.section.<id>.keywords`).
 * Best match first, so Return opens the obvious section; ties keep the rail
 * order. An empty query keeps every section in rail order.
 */
export function matchSections(
  query: string,
  describe: (id: SettingsSection) => { name: string; keywords: string },
): SettingsSection[] {
  const all = SETTINGS_GROUPS.flatMap((g) => g.sections)
  if (searchWords(query).length === 0) return all
  return all
    .map((id, order) => ({ id, order, score: sectionScore(query, describe(id)) }))
    .filter((m): m is { id: SettingsSection; order: number; score: [number, number] } =>
      Boolean(m.score),
    )
    .sort((a, b) => a.score[0] - b.score[0] || a.score[1] - b.score[1] || a.order - b.order)
    .map((m) => m.id)
}
