import type { NotifySound } from '@shared/notify'
import { desktopApi, isDesktop } from './desktop-api'

/**
 * How the shell makes a sound. The two-note chime is synthesised here (no
 * audio asset, nothing to license or bundle); the macOS alert sounds are
 * played by main. Whether to play at all is the dispatcher's call.
 */

let audioCtx: AudioContext | null = null

function context(): AudioContext | null {
  try {
    audioCtx ??= new AudioContext()
    if (audioCtx.state === 'suspended') void audioCtx.resume()
    return audioCtx
  } catch {
    return null
  }
}

/** Two soft sine notes a fifth apart, ~350ms. Reads as "done", not "alert". */
export function playChime(): void {
  const ctx = context()
  if (!ctx) return
  const t0 = ctx.currentTime
  const master = ctx.createGain()
  master.gain.value = 0.16
  master.connect(ctx.destination)
  const notes: [number, number][] = [
    [659.25, 0], // E5
    [987.77, 0.11], // B5
  ]
  for (const [freq, at] of notes) {
    const osc = ctx.createOscillator()
    const env = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    env.gain.setValueAtTime(0, t0 + at)
    env.gain.linearRampToValueAtTime(1, t0 + at + 0.012)
    env.gain.exponentialRampToValueAtTime(0.001, t0 + at + 0.32)
    osc.connect(env)
    env.connect(master)
    osc.start(t0 + at)
    osc.stop(t0 + at + 0.34)
  }
}

/** The chosen sound; a system sound falls back to the chime outside the app. */
export function playSound(name: NotifySound): void {
  if (name === 'chime' || !isDesktop()) {
    playChime()
    return
  }
  void desktopApi().notify.sound(name)
}

/** True when the window is not the one the user is looking at. */
export function windowInBackground(): boolean {
  return document.visibilityState === 'hidden' || !document.hasFocus()
}
