/**
 * The notification vocabulary shared by main (which shows them), preload and
 * the renderer (which decides when). Settings live in `settings.ts`; this
 * file is the shape of one notification and the sounds it may make.
 */

/** What happened. Drives the icon in the bell list and the per-event toggle. */
export type NotifyKind =
  'reply' | 'replyFailed' | 'approval' | 'job' | 'jobFailed' | 'gateway' | 'test'

/** Where a click should land. */
export type NotifyTarget =
  | { type: 'session'; key: string }
  | { type: 'jobs'; jobId?: string }
  | { type: 'approvals' }
  | { type: 'settings' }
  | { type: 'none' }

/** What main needs to show one native notification. */
export interface NotifyRequest {
  /** Stable per logical event so a repeat replaces instead of stacking. */
  tag: string
  kind: NotifyKind
  title: string
  /** macOS renders this under the title; other paths fold it into the body. */
  subtitle?: string
  body?: string
  target: NotifyTarget
}

export interface NotifyResult {
  /** False when the platform has no notification support or posting threw. */
  shown: boolean
}

/**
 * macOS alert sounds, by the file name in /System/Library/Sounds. Played by
 * main through `afplay`, so the choice is real even when no notification is
 * posted (the window is in front). `chime` is the app's own two-note synth.
 */
export const SYSTEM_SOUNDS = [
  'Glass',
  'Ping',
  'Pop',
  'Purr',
  'Tink',
  'Hero',
  'Submarine',
  'Blow',
  'Bottle',
  'Funk',
  'Morse',
  'Sosumi',
  'Basso',
  'Frog',
] as const
export type SystemSound = (typeof SYSTEM_SOUNDS)[number]
export type NotifySound = 'chime' | SystemSound

export function isSystemSound(value: unknown): value is SystemSound {
  return typeof value === 'string' && (SYSTEM_SOUNDS as readonly string[]).includes(value)
}

export function isNotifySound(value: unknown): value is NotifySound {
  return value === 'chime' || isSystemSound(value)
}
