/** Sessions-list filter state, kept apart so the eager shell store stays small. */
export interface SessionFilters {
  text: string
  agent: string | null
  channel: string | null
  project: string | null
}

export const NO_FILTERS: SessionFilters = { text: '', agent: null, channel: null, project: null }

export function hasActiveFilters(filters: SessionFilters): boolean {
  return Boolean(filters.text.trim() || filters.agent || filters.channel || filters.project)
}
