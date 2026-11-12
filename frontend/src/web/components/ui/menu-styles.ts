// Shared item classes for the dropdown and context menus. Menus float on the
// overlay surface with a hairline and a layered shadow, and grow out of their
// trigger (`anim-pop` reads Radix's transform-origin variable).
export const menuContentClass = [
  'z-[var(--z-popover)] min-w-48 overflow-hidden rounded-[12px] border border-border bg-surface-overlay p-1 text-text shadow-overlay',
  'anim-pop',
].join(' ')

export const menuItemClass = [
  'target relative flex min-h-8 cursor-default items-center gap-2.5 rounded-[8px] px-2.5 py-1.5 text-[13.5px] outline-none select-none',
  'transition-colors duration-[var(--duration-fast)]',
  'focus:bg-surface-3 data-[highlighted]:bg-surface-3',
  // surface-3 alone is ~1.1:1 against the menu: keyboard focus also gets the ring.
  'focus-visible:outline-solid focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring',
  'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
  'data-[variant=danger]:text-danger data-[variant=danger]:data-[highlighted]:bg-danger/10',
  '[&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-text-muted data-[variant=danger]:[&_svg]:text-danger',
].join(' ')

export const menuLabelClass = 'px-2.5 pt-2 pb-1 text-label text-text-muted'
export const menuSeparatorClass = '-mx-1 my-1 h-px bg-border'
