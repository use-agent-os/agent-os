import { Eye, EyeOff } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { create } from 'zustand'
import { t } from '@/i18n'
import '@/i18n/en/webSettings'
import { cn } from '@/lib/utils'
import { Button } from '@/web/components/ui/button'
import { Input, Select, Textarea } from '@/web/components/ui/input'
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
  detail,
  help,
  error,
  children,
  inputId,
}: {
  id: string
  label: string
  /** Secondary text shown with the label (Advanced: the schema title under its path). */
  detail?: string
  help?: string
  error?: string
  children: React.ReactNode
  inputId: string
}) {
  return (
    <div data-field-id={id} className="flex flex-col gap-1 py-3">
      <label htmlFor={inputId} className="flex flex-wrap items-baseline gap-x-2 font-medium">
        {label}
        {detail ? (
          <>
            {' '}
            <span className="text-small font-normal text-text-muted">{detail}</span>
          </>
        ) : null}
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

const inputClass = 'max-w-xl'

/** The `<select>` value standing for "unset: use the gateway default". */
const UNSET = ''

/**
 * One configuration value bound to the section's editor. An `optional` value
 * can be unset again: booleans and enums get a "Default" choice, and an
 * emptied text field is written as null rather than "".
 */
export function ConfigField({
  editor,
  path,
  label,
  detail,
  help,
  type,
  optional,
  disabled,
}: {
  editor: ConfigEditor
  path: string
  label: string
  detail?: string
  help?: string
  type: FieldType
  optional?: boolean
  disabled?: boolean
}) {
  const inputId = useId()
  const [shown, toggleShown] = useReveal()
  // The text being typed into a JSON field, tied to the editor generation it
  // was typed in: a discard, reload or save drops it.
  const [jsonDraft, setJsonDraft] = useState<{ generation: number; text: string } | null>(null)
  const draftText = jsonDraft?.generation === editor.generation ? jsonDraft.text : null
  const value = editor.value(path)
  const jsonInvalid = editor.invalid.includes(path)
  const error =
    editor.state.kind === 'invalid'
      ? editor.state.fields[path]
      : jsonInvalid
        ? t('webSettings.jsonInvalid')
        : undefined
  const describedBy =
    [help ? `${inputId}-help` : '', error ? `${inputId}-error` : ''].filter(Boolean).join(' ') ||
    undefined
  const common = {
    id: inputId,
    'aria-describedby': describedBy,
    'aria-invalid': error ? true : undefined,
    disabled: disabled || type.kind === 'readonly',
  }
  const unset = value === null || value === undefined

  let control: React.ReactNode
  switch (type.kind) {
    case 'boolean':
      control = optional ? (
        <Select
          {...common}
          className={inputClass}
          value={unset ? UNSET : value ? 'true' : 'false'}
          onChange={(e) =>
            editor.set(path, e.target.value === UNSET ? null : e.target.value === 'true')
          }
        >
          <option value={UNSET}>{t('webSettings.optionDefault')}</option>
          <option value="true">{t('webSettings.optionOn')}</option>
          <option value="false">{t('webSettings.optionOff')}</option>
        </Select>
      ) : (
        <Switch {...common} checked={Boolean(value)} onCheckedChange={(v) => editor.set(path, v)} />
      )
      break
    case 'number':
      control = (
        <Input
          {...common}
          type="number"
          className={inputClass}
          value={unset ? '' : String(value)}
          onChange={(e) => editor.set(path, e.target.value === '' ? null : Number(e.target.value))}
        />
      )
      break
    case 'enum':
      control = (
        <Select
          {...common}
          className={inputClass}
          value={unset ? UNSET : String(value)}
          onChange={(e) => editor.set(path, e.target.value === UNSET ? null : e.target.value)}
        >
          {optional || unset ? (
            <option value={UNSET} disabled={!optional}>
              {t('webSettings.optionDefault')}
            </option>
          ) : null}
          {type.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </Select>
      )
      break
    case 'json':
      control = (
        <Textarea
          {...common}
          rows={4}
          className={cn(inputClass, 'font-mono text-small')}
          value={draftText ?? JSON.stringify(value ?? null, null, 2)}
          onChange={(e) => {
            const text = e.target.value
            setJsonDraft({ generation: editor.generation, text })
            let parsed: unknown
            try {
              parsed = JSON.parse(text)
            } catch {
              // Not a value yet: block Save rather than save the last one that parsed.
              editor.setInvalid(path, true)
              return
            }
            editor.setInvalid(path, false)
            editor.set(path, parsed)
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
          <Input
            {...common}
            type={type.secret && !shown ? 'password' : 'text'}
            autoComplete="off"
            className={inputClass}
            value={unset ? '' : String(value)}
            placeholder={optional && unset ? t('webSettings.placeholderDefault') : undefined}
            onChange={(e) =>
              editor.set(path, optional && e.target.value === '' ? null : e.target.value)
            }
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
    <FieldRow id={path} label={label} detail={detail} help={help} error={error} inputId={inputId}>
      {control}
    </FieldRow>
  )
}
