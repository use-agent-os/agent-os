// Shared setup-view building blocks: the catalog-driven field renderer, the
// "what you need" list, capability readiness badge, section panel head, and the
// CLI env-recovery command row. Kept dumb — all decisions live in logic.ts and
// each section owns its state; these only render.
import { CheckIcon, ChevronDownIcon } from 'lucide-react'
import type { ReactNode, SelectHTMLAttributes } from 'react'
import { CommandLine } from '@/components/CommandLine'
import { capabilityBadge, type FieldSpec, type OnboardingStatus } from './logic'

// setup.js:307-315 — the "what you need" list (hidden when empty).
export function NeedList({ items, label }: { items: string[] | undefined; label: string }) {
  const needs = (items || []).filter(Boolean)
  if (!needs.length) return null
  return (
    <div className="setup-needs" aria-label={label}>
      <span className="t-label">{label}</span>
      <ul>
        {needs.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  )
}

// setup.js:966-969 — capability readiness badge (tone via --tone class).
const BADGE_TONE: Record<string, string> = {
  'is-ok': 'tone-ok',
  'is-warn': 'tone-warn',
  'is-muted': 'tone-dim',
}
export function CapabilityBadge({ status, name }: { status: OnboardingStatus; name: string }) {
  const badge = capabilityBadge(status, name)
  return <span className={`setup-badge ${BADGE_TONE[badge.tone]}`}>{badge.label}</span>
}

// setup.js:520-548 — an env-recovery command with a copy affordance (common CommandLine).
export function EnvRecoveryCommand({ command }: { command: string }) {
  if (!command) return null
  return (
    <div className="setup-env-command">
      <CommandLine command={command} toastIdPrefix="setup-copy" />
    </div>
  )
}

// setup.js:374-377 — a section panel head (title + subtitle).
export function PanelHead({ title, subtitle }: { title: string; subtitle: React.ReactNode }) {
  return (
    <header className="setup-panel__head">
      <h2 className="t-label">{title}</h2>
      <p className="setup-panel__subtitle">{subtitle}</p>
    </header>
  )
}

export function SetupSelect({ children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className="setup-select">
      <select {...props}>{children}</select>
      <ChevronDownIcon aria-hidden="true" size={16} strokeWidth={2} />
    </span>
  )
}

export function SetupCheckbox({
  ariaLabel,
  checked,
  className = '',
  disabled = false,
  children,
  onChange,
}: {
  ariaLabel: string
  checked: boolean
  className?: string
  disabled?: boolean
  children: ReactNode
  onChange: (checked: boolean) => void
}) {
  return (
    <label className={`setup-check ${className}`.trim()}>
      <input
        className="setup-check__input"
        type="checkbox"
        aria-label={ariaLabel}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="setup-check__control" aria-hidden="true">
        <CheckIcon size={14} strokeWidth={2.5} />
      </span>
      <span className="setup-check__label">{children}</span>
    </label>
  )
}

/**
 * setup.js:1290-1310 — a catalog-driven field. Secrets render type=password with
 * NO value (never echoed/logged). Value/onChange are controlled by the section.
 * `showWhen` visibility is resolved by the caller (passed as `hidden`).
 */
export function SetupField({
  field,
  value,
  checked,
  hidden,
  onChange,
  onToggle,
}: {
  field: FieldSpec
  value: string
  checked: boolean
  hidden: boolean
  onChange: (value: string) => void
  onToggle: (checked: boolean) => void
}) {
  if (hidden) return null
  const required = field.required ? ' *' : ''
  const desc = field.description ? (
    <small className="setup-field-desc">{field.description}</small>
  ) : null

  if (field.type === 'bool') {
    return (
      <SetupCheckbox ariaLabel={field.label || field.name} checked={checked} onChange={onToggle}>
        <>
          {field.label}
          {required}
          {desc}
        </>
      </SetupCheckbox>
    )
  }

  if (field.type === 'select') {
    return (
      <label>
        <span>
          {field.label}
          {required}
        </span>
        {desc}
        <SetupSelect value={value} onChange={(e) => onChange(e.target.value)}>
          {(field.choices || []).map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </SetupSelect>
      </label>
    )
  }

  const isSecret = field.secret || field.type === 'password'
  const inputType = isSecret
    ? 'password'
    : field.type === 'int' || field.type === 'float'
      ? 'number'
      : 'text'
  const placeholder = field.placeholder || (isSecret ? 'leave blank to keep current' : '')
  return (
    <label>
      <span>
        {field.label}
        {required}
      </span>
      {desc}
      <input
        type={inputType}
        // Secrets are write-only: the input never shows a stored value.
        value={isSecret ? undefined : value}
        defaultValue={isSecret ? '' : undefined}
        aria-label={field.label || field.name}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}
