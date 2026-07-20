import { useEffect, useRef } from 'react'

// Shared modal shell for the tokenized dialogs across the agents / sessions /
// skills views. Superset of the three previously-duplicated inline shells:
//   - role: 'dialog' | 'alertdialog' (destructive confirms use alertdialog)
//   - aria-labelledby / optional aria-describedby
//   - focus the first focusable control on open (input,textarea,select,button)
//   - Escape closes (stopPropagation so a nested confirm doesn't also bubble up)
//   - backdrop mousedown closes (only when the press starts on the overlay)
// Each view keeps its own scoped CSS by passing overlayClassName + className
// (e.g. ag-modal / sess-modal / sk-modal), so the extraction is behavior-only.
export function ModalShell({
  role,
  labelledBy,
  describedBy,
  onClose,
  overlayClassName,
  className,
  children,
}: {
  role: 'dialog' | 'alertdialog'
  labelledBy: string
  describedBy?: string
  onClose: () => void
  overlayClassName: string
  className?: string
  children: React.ReactNode
}) {
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Focus the first focusable control for keyboard users.
    const first = panelRef.current?.querySelector<HTMLElement>(
      'input:not([disabled]), textarea, select, button',
    )
    first?.focus()
  }, [])

  return (
    <div
      className={overlayClassName}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        className={className}
        role={role}
        aria-modal="true"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            onClose()
          }
        }}
      >
        {children}
      </div>
    </div>
  )
}
