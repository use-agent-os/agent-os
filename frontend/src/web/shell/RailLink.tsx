import { forwardRef, type ComponentProps } from 'react'
import { Link, useMatch, useResolvedPath } from 'react-router'
import { cn } from '@/lib/utils'
import { buttonVariants } from '@/web/components/ui/button'

/**
 * An icon link for the rail. A plain-string className (not NavLink's function
 * form) so it survives Radix `Slot` merging when wrapped in a tooltip trigger.
 */
export const RailLink = forwardRef<HTMLAnchorElement, ComponentProps<typeof Link>>(
  function RailLink({ to, className, ...props }, ref) {
    const resolved = useResolvedPath(to)
    const active = useMatch({ path: resolved.pathname, end: false }) !== null
    return (
      <Link
        ref={ref}
        to={to}
        aria-current={active ? 'page' : undefined}
        className={cn(
          buttonVariants({ variant: 'ghost', size: 'icon' }),
          'relative',
          active && 'bg-surface-3',
          className as string | undefined,
        )}
        {...props}
      />
    )
  },
)
