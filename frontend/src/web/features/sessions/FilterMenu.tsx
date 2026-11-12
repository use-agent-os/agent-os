import { Check, ChevronDown } from 'lucide-react'
import type { ReactNode } from 'react'
import { formatNumber, t } from '@/i18n'
import '@/i18n/en/webPagesWork'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItemIndicator,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/web/components/ui/dropdown-menu'

export interface FilterOption {
  value: string
  label: string
  /** How many rows the option matches, shown on the right. */
  count?: number
  /** A leading mark: a status dot, an icon. */
  mark?: ReactNode
}

/**
 * A filter chip: names its filter, shows the chosen value once one is set,
 * and opens a single-choice menu. The first option is the "everything"
 * choice; picking it clears the filter. Replaces a labelled native select in
 * a toolbar.
 */
export function FilterMenu({
  label,
  icon,
  value,
  options,
  onChange,
}: {
  label: string
  icon: ReactNode
  value: string
  options: FilterOption[]
  onChange: (value: string) => void
}) {
  const [all, ...rest] = options
  const active = value !== all?.value
  const chosen = options.find((o) => o.value === value) ?? all
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          data-filter-trigger
          data-active={active || undefined}
          aria-label={t('webPagesWork.filterChip', { label, value: chosen?.label ?? '' })}
          className="sx-chip press target"
        >
          <span aria-hidden className="sx-chip-icon">
            {icon}
          </span>
          <span className="text-text-muted">{label}</span>
          {active ? <span className="sx-chip-value">{chosen?.label}</span> : null}
          <ChevronDown aria-hidden className="sx-chip-caret" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-80 min-w-56 overflow-y-auto">
        <DropdownMenuLabel>{t('webPagesWork.filterMenu', { label })}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={value} onValueChange={onChange}>
          {all ? <FilterItem option={all} /> : null}
          {rest.length ? <DropdownMenuSeparator /> : null}
          {rest.map((option) => (
            <FilterItem key={option.value} option={option} />
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function FilterItem({ option }: { option: FilterOption }) {
  return (
    <DropdownMenuRadioItem value={option.value} className="pr-2.5 pl-2">
      <span className="grid size-4 shrink-0 place-items-center">
        <DropdownMenuItemIndicator>
          <Check className="!text-accent" aria-hidden />
        </DropdownMenuItemIndicator>
      </span>
      {option.mark ? (
        <span aria-hidden className="grid size-4 shrink-0 place-items-center">
          {option.mark}
        </span>
      ) : null}
      <span className="min-w-0 flex-1 truncate">{option.label}</span>
      {option.count !== undefined ? (
        <span className="ml-3 text-small text-text-muted tabular-nums">
          {formatNumber(option.count)}
        </span>
      ) : null}
    </DropdownMenuRadioItem>
  )
}
