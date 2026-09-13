/**
 * Petdex pets, the format Hermes and Codex share: a `pet.json` beside one
 * spritesheet, a grid of 192×208 frames, one row per animation state, six
 * frames stepped over 1.1s. See https://github.com/crafter-station/petdex.
 */

export const PET_FRAME_W = 192
export const PET_FRAME_H = 208
export const PET_FRAMES_PER_STATE = 6
export const PET_LOOP_MS = 1100

export const PET_MIN_SCALE = 0.1
export const PET_MAX_SCALE = 3
export const PET_DEFAULT_SCALE = 0.33

export function clampPetScale(scale: number): number {
  if (!Number.isFinite(scale)) return PET_DEFAULT_SCALE
  return Math.min(PET_MAX_SCALE, Math.max(PET_MIN_SCALE, scale))
}

/** A slug is a petdex id: what the manifest calls it and the folder it lives in. */
export function isPetSlug(value: unknown): value is string {
  return typeof value === 'string' && /^[a-z0-9][a-z0-9-]{0,80}$/.test(value)
}

/** What the pet is doing on screen. Hermes' activity names. */
export type PetState = 'idle' | 'wave' | 'run' | 'failed' | 'review' | 'jump' | 'waiting'

/** Current petdex sheets: 8 columns × 9 rows (v1) or × 11 rows (v2, two custom rows). */
const CODEX_ROWS = [
  'idle',
  'running-right',
  'running-left',
  'waving',
  'jumping',
  'failed',
  'waiting',
  'running',
  'review',
] as const

/** Older 9 × 8 sheets. */
const LEGACY_ROWS = ['idle', 'wave', 'run', 'failed', 'review', 'jump', 'extra1', 'extra2'] as const

const ALIASES: Record<PetState, readonly string[]> = {
  idle: ['idle'],
  wave: ['wave', 'waving'],
  jump: ['jump', 'jumping'],
  run: ['run', 'running'],
  failed: ['failed'],
  review: ['review'],
  waiting: ['waiting'],
}

export interface SheetGeometry {
  cols: number
  rows: number
}

/** Frame grid from the sheet's pixel size; a sheet that is not a whole grid is rejected. */
export function sheetGeometry(width: number, height: number): SheetGeometry | null {
  const cols = width / PET_FRAME_W
  const rows = height / PET_FRAME_H
  if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1) return null
  return { cols, rows }
}

/**
 * Real frames per row. Petdex sheets are left-packed: a state with fewer
 * frames than the grid is wide leaves the rest of its row transparent, so
 * animating across the whole row blinks the pet out. Stop at the first
 * blank frame, and never step more than PET_FRAMES_PER_STATE.
 */
export function rowFrameCounts(
  geometry: SheetGeometry,
  isBlank: (col: number, row: number) => boolean,
): number[] {
  const counts: number[] = []
  for (let row = 0; row < geometry.rows; row++) {
    let n = 0
    while (n < Math.min(geometry.cols, PET_FRAMES_PER_STATE) && !isBlank(n, row)) n++
    counts.push(Math.max(1, n))
  }
  return counts
}

/** The sheet row that animates `state`; idle when the sheet has no such row. */
export function petStateRow(state: PetState, rows: number): number {
  const taxonomy: readonly string[] = rows >= CODEX_ROWS.length ? CODEX_ROWS : LEGACY_ROWS
  for (const name of ALIASES[state]) {
    const i = taxonomy.indexOf(name)
    if (i >= 0 && i < rows) return i
  }
  return 0
}

export interface PetSignals {
  /** A tool or the turn just failed. */
  error?: boolean
  /** An explicit success beat (a plan finished). */
  celebrate?: boolean
  /** The turn finished cleanly a moment ago. */
  justCompleted?: boolean
  /** Blocked on the user: an approval or a question is open. */
  awaitingInput?: boolean
  toolRunning?: boolean
  reasoning?: boolean
  busy?: boolean
}

/**
 * One row shows at a time, so the most salient signal wins. Same priority
 * as Hermes' `derive_pet_state`, so a pet behaves the same in both apps.
 */
export function derivePetState(s: PetSignals): PetState {
  if (s.error) return 'failed'
  if (s.celebrate) return 'jump'
  if (s.justCompleted) return 'wave'
  if (s.awaitingInput) return 'waiting'
  if (s.toolRunning) return 'run'
  if (s.reasoning) return 'review'
  if (s.busy) return 'run'
  return 'idle'
}

/** A row of the public manifest (petdex.dev/api/manifest). */
export interface PetManifestEntry {
  slug: string
  displayName: string
  kind: string
  submittedBy: string
  spriteVersion: number
}

/** A pet on disk in the app's pets directory. */
export interface InstalledPet {
  slug: string
  displayName: string
  description: string
  /** Where the renderer loads the sheet from (`agentos-pet://sheet/<slug>`). */
  sheetUrl: string
}

export const PET_SCHEME = 'agentos-pet'

export function petSheetUrl(slug: string): string {
  return `${PET_SCHEME}://sheet/${slug}`
}
