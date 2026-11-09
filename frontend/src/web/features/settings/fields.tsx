import { Eye, EyeOff } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { create } from 'zustand'
import { t } from '@/i18n'
import '@/i18n/en/webSettings'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { Switch } from '@/web/components/ui/switch'
import type { ConfigEditor } from './config-editor'

// Settings fields and the field registry settings search reads.

export interface RegisteredField {
  id: string
  label: string
}

export const useSettingsFields = create<{
  bySection: Record<string, RegisteredField[]>
  register(section: string, fields: RegisteredField[]): void
}>((set) => ({
  bySection: {},
  register: (section, fields) => set((s) => ({ bySection: { ...s.bySection, [section]: fields } })),
}))

/** Register a section's field labels for settings search. */
export function useRegisterFields(section: string, fields: RegisteredField[]): void {
  const signature = fields.map((f) => `${f.id}:${f.label}`).join('|')
  const register = useSettingsFields((s) => s.register)
  useEffect(() => {
    register(section, fields)
    // Keyed on content, not array identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section, signature, register])
}

/** Masks a secret until revealed; hides it again after `timeoutMs`. */
export function useReveal(timeoutMs = 15_000): [boolean, () => void] {
  const [shown, setShown] = useState(false)
  useEffect(() => {
    if (!shown) return
    const id = window.setTimeout(() => setShown(false), timeoutMs)
    return () => window.clearTimeout(id)
  }, [shown, timeoutMs])
  return [shown, () => setShown((v) => !v)]
}

export type FieldType =
  | { kind: 'boolean' }
  | { kind: 'number' }
  | { kind: 'string'; secret?: boolean; multiline?: boolean }
  | { kind: 'enum'; options: readonly string[] }
  | { kind: 'readonly' }
  | { kind: 'json' }

export function FieldRow({
  id,
  label,
  help,
  error,
  children,
  inputId,
}: {
  id: string
  label: string
  help?: string
  error?: string
  children: React.ReactNode
  inputId: string
}) {
  return (
    <div data-field-id={id} className="flex flex-col gap-1 py-3">
      <label htmlFor={inputId} className="font-medium">
        {label}
      </label>
      {help ? (
        <p id={`${inputId}-help`} className="text-small text-text-muted">
          {help}
        </p>
      ) : null}
      {children}
      {error ? (
        <p id={`${inputId}-error`} role="alert" className="text-small text-danger">
          {error}
        </p>
      ) : null}
    </div>
  )
}

const inputClass =
  'h-9 w-full max-w-xl rounded-md border border-border-control bg-surface-0 px-3 aria-[invalid=true]:border-danger touch:h-11 touch:text-base'

/** One configuration value bound to the section's editor. */
export function ConfigField({
  editor,
  path,
  label,
  help,
  type,
  disabled,
}: {
  editor: ConfigEditor
  path: string
  label: string
  help?: string
  type: FieldType
  disabled?: boolean
}) {
  const inputId = useId()
  const [shown, toggleShown] = useReveal()
  const [jsonDraft, setJsonDraft] = useState<string | null>(null)
  const value = editor.value(path)
  const error = editor.state.kind === 'invalid' ? editor.state.fields[path] : undefined
  const describedBy =
    [help ? `${inputId}-help` : '', error ? `${inputId}-error` : ''].filter(Boolean).join(' ') ||
    undefined
  const common = {
    id: inputId,
    'aria-describedby': describedBy,
    'aria-invalid': error ? true : undefined,
    disabled: disabled || type.kind === 'readonly',
  }

  let control: React.ReactNode
  switch (type.kind) {
    case 'boolean':
      control = (
        <Switch {...common} checked={Boolean(value)} onCheckedChange={(v) => editor.set(path, v)} />
      )
      break
    case 'number':
      control = (
        <input
          {...common}
          type="number"
          className={inputClass}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(e) => editor.set(path, e.target.value === '' ? null : Number(e.target.value))}
        />
      )
      break
    case 'enum':
      control = (
        <select
          {...common}
          className={inputClass}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(e) => editor.set(path, e.target.value)}
        >
          {type.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      )
      break
    case 'json':
      control = (
        <textarea
          {...common}
          rows={4}
          className={cn(inputClass, 'h-auto py-2 font-mono text-small')}
          value={jsonDraft ?? JSON.stringify(value ?? null, null, 2)}
          onChange={(e) => {
            setJsonDraft(e.target.value)
            try {
              editor.set(path, JSON.parse(e.target.value))
            } catch {
              /* keep the draft until it parses */
            }
          }}
        />
      )
      break
    case 'readonly':
      control = (
        <output id={inputId} className="font-mono text-small">
          {JSON.stringify(value)}
        </output>
      )
      break
    default:
      control = (
        <div className="flex max-w-xl items-center gap-2">
          <input
            {...common}
            type={type.secret && !shown ? 'password' : 'text'}
            autoComplete="off"
            className={inputClass}
            value={value === null || value === undefined ? '' : String(value)}
            onChange={(e) => editor.set(path, e.target.value)}
          />
          {type.secret ? (
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={shown ? t('webSettings.hide') : t('webSettings.reveal')}
              onClick={toggleShown}
            >
              {shown ? <EyeOff /> : <Eye />}
            </Button>
          ) : null}
        </div>
      )
  }
  return (
    <FieldRow id={path} label={label} help={help} error={error} inputId={inputId}>
      {control}
    </FieldRow>
  )
}
