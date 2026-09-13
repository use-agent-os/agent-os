import type { Transition } from 'motion/react'

/**
 * Motion vocabulary for the shell. Two curves, used everywhere:
 *
 *  - `spring`: macOS-style interactive spring for things that move between
 *    places (the composer docking, sheets). Slightly under-damped so the
 *    settle reads as physical, never bouncy.
 *  - `ease`: Apple's standard cubic-bezier for fades and small offsets.
 */
export const spring: Transition = { type: 'spring', stiffness: 340, damping: 34, mass: 0.9 }
export const ease: Transition = { duration: 0.32, ease: [0.32, 0.72, 0, 1] }
export const quick: Transition = { duration: 0.18, ease: [0.32, 0.72, 0, 1] }
