// Shared item classes for the dropdown and context menus.
export const menuContentClass = [
  'z-[var(--z-popover)] min-w-44 overflow-hidden rounded-lg border border-border bg-surface-2 p-1 text-text shadow-2',
  'anim-overlay-in',
].join(' ')

export const menuItemClass = [
  'target relative flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-ui outline-none select-none',
  'focus:bg-surface-3 data-[highlighted]:bg-surface-3',
  'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
  'data-[variant=danger]:text-danger [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-text-muted',
].join(' ')

export const menuLabelClass = 'px-2 py-1.5 text-small font-medium text-text-muted'
export const menuSeparatorClass = '-mx-1 my-1 h-px bg-border'
