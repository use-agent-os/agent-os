import { motion, useReducedMotion, type Transition, type Variants } from 'motion/react'
import type { ReactNode } from 'react'

// Shared motion primitives (Subtle tier, ui-ux-pro-max). Terminal-flavored:
// crisp and mechanical, small offsets, quick settle — never floaty. Consumers
// also honor useReducedMotion() and degrade to instant when it's true.

// Quick, low-bounce spring for panels/dialogs.
export const SUBTLE_SPRING: Transition = {
  type: 'spring',
  stiffness: 420,
  damping: 34,
  mass: 0.6,
}

// Duration+ease pairing for list rows (used directly by sessions' motion.tr).
export const SUBTLE_EASE: Transition = {
  duration: 0.22,
  ease: [0.16, 1, 0.3, 1],
}

// Modal overlay: FADE ONLY (no transform). A transform here would create a
// containing block / shift the panel, so the overlay stays a clean flex
// centering context for its panel child.
export const overlayVariants: Variants = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
}

// Modal panel: fade + a small scale settle from its own center. Crucially it
// does NOT animate `y` — animating y fights the overlay's align-items:center
// and makes the dialog appear anchored to the bottom of the viewport.
export const panelVariants: Variants = {
  initial: { opacity: 0, scale: 0.98 },
  animate: { opacity: 1, scale: 1 },
  exit: { opacity: 0, scale: 0.98 },
}

// List item enter/exit for AnimatePresence + layout. Small vertical rise so
// rows "boot in"; transform + opacity only.
export const listItemVariants: Variants = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -4 },
}

// Wrapper for list/grid items: animates in/out via AnimatePresence and slides
// neighbors with `layout` when items are added/removed/reordered. Renders a
// plain motion.div so it drops into card grids and flex/grid lists. Reduced
// motion → no transition (instant), layout still snaps without tweening.
export function MotionListItem({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  const reduce = useReducedMotion()
  return (
    <motion.div
      className={className}
      layout={!reduce}
      variants={listItemVariants}
      initial="initial"
      animate="animate"
      exit="exit"
      transition={reduce ? { duration: 0 } : SUBTLE_EASE}
    >
      {children}
    </motion.div>
  )
}
