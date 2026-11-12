import { forwardRef, type ComponentProps } from 'react'
import { Link, useMatch, useResolvedPath } from 'react-router'
import { cn } from '@/lib/utils'
import { buttonVariants } from '@/web/components/ui/button'

/**
 * An icon link for the rail. A plain-string className (not NavLink's function
 * form) so it survives Radix `Slot` merging when wrapped in a tooltip trigger.
 * Navigating cross-fades the main area (base.css page transitions); the active
 * background is the rail group's gliding pill.
 */
export const RailLink = forwardRef<HTMLAnchorElement, ComponentProps<typeof Link>>(
  function RailLink({ to, className, ...props }, ref) {
    const resolved = useResolvedPath(to)
    const active = useMatch({ path: resolved.pathname, end: false }) !== null
    return (
      <Link
        ref={ref}
        to={to}
        viewTransition
        aria-current={active ? 'page' : undefined}
        className={cn(
          buttonVariants({ variant: 'ghost', size: 'icon' }),
          'rail-item relative',
          className as string | undefined,
        )}
        {...props}
      />
    )
  },
)
