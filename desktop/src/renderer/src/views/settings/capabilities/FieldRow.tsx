import { Eye, EyeOff } from 'lucide-react'
import { useId, useState, type ReactNode } from 'react'
import type { FieldSpec } from '@/views/setup/logic'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t, type MessageKey } from '~/i18n'
import { Row } from '../parts'
import { isSecretField, str } from './logic'

/** Plain names for the catalog's field ids; anything else keeps the catalog label. */
const LABEL: Record<string, MessageKey> = {
  api_key: 'settings.capabilities.field.api_key',
  api_key_env: 'settings.capabilities.field.api_key_env',
  max_results: 'settings.capabilities.field.max_results',
  primary: 'settings.capabilities.field.primary',
  model: 'settings.capabilities.field.model',
  base_url: 'settings.capabilities.field.base_url',
  proxy: 'settings.capabilities.field.proxy',
  use_env_proxy: 'settings.capabilities.field.use_env_proxy',
  fallback_policy: 'settings.capabilities.field.fallback_policy',
  diagnostics: 'settings.capabilities.field.diagnostics',
  tts_voice: 'settings.capabilities.field.tts_voice',
  tts_model: 'settings.capabilities.field.tts_model',
  language_code: 'settings.capabilities.field.language_code',
  reasoning_effort: 'settings.capabilities.field.reasoning_effort',
  timeout_seconds: 'settings.capabilities.field.timeout_seconds',
  total_timeout_seconds: 'settings.capabilities.field.total_timeout_seconds',
  retries: 'settings.capabilities.field.retries',
}

const HELP: Record<string, MessageKey> = {
  api_key_env: 'settings.capabilities.field.api_key_env.help',
  max_results: 'settings.capabilities.field.max_results.help',
  base_url: 'settings.capabilities.field.base_url.help',
  primary: 'settings.capabilities.field.primary.help',
}

const CHOICE: Record<string, Record<string, MessageKey>> = {
  fallback_policy: {
    off: 'settings.capabilities.field.fallback_policy.off',
    network: 'settings.capabilities.field.fallback_policy.network',
  },
  reasoning_effort: { '': 'settings.capabilities.field.reasoning_effort.default' },
}

export function fieldLabel(field: FieldSpec): string {
  const key = LABEL[field.name]
  return key ? t(key) : field.label || field.name
}

function choiceLabel(field: FieldSpec, value: string): string {
  const key = CHOICE[field.name]?.[value]
  if (key) return t(key)
  return value === '' ? t('settings.capabilities.optionDefault') : value
}

/** Machine values (ids, URLs, variable names) read better in mono. */
const MONO = new Set(['api_key_env', 'base_url', 'proxy', 'primary', 'model', 'tts_voice'])
/** Long machine values: the input gets room for a full model id or URL. */
const WIDE = new Set(['primary', 'base_url', 'proxy'])

export function FieldRow({
  field,
  value,
  onChange,
  disabled,
  help,
  placeholder,
  suggestions,
  after,
  error,
}: {
  field: FieldSpec
  value: unknown
  onChange: (value: unknown) => void
  disabled?: boolean
  /** Replaces the catalog help (the key row says where the key comes from). */
  help?: ReactNode
  /** What is wrong with the value, said in place of the help. */
  error?: string
  placeholder?: string
  /** Offered in the input's list (image models). */
  suggestions?: readonly string[]
  /** Right of the control (Get a key). */
  after?: ReactNode
}) {
  const id = useId()
  const [shown, setShown] = useState(false)
  const label = fieldLabel(field)
  const errorId = `${id}-error`
  const helpText = error ? (
    <span className="cap-field-error" id={errorId} role="alert">
      {error}
    </span>
  ) : (
    (help ??
    (HELP[field.name] ? t(HELP[field.name]!) : field.description || field.help || undefined))
  )
  const invalid = error
    ? ({ 'aria-invalid': true, 'aria-describedby': errorId } as const)
    : undefined
  const text = str(value)

  if (field.type === 'bool') {
    return (
      <Row label={label} help={helpText}>
        <Switch
          checked={Boolean(value)}
          disabled={disabled}
          aria-label={label}
          onCheckedChange={(v) => onChange(v)}
        />
      </Row>
    )
  }

  if (field.type === 'select' && field.choices?.length) {
    return (
      <Row label={label} help={helpText} htmlFor={id}>
        <select
          id={id}
          className="mac-select"
          data-compact="true"
          value={text}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          {field.choices.map((c) => (
            <option key={c} value={c}>
              {choiceLabel(field, c)}
            </option>
          ))}
        </select>
      </Row>
    )
  }

  if (isSecretField(field)) {
    return (
      <Row label={label} help={helpText} htmlFor={id} align="start">
        <span className="stg-input-wrap cap-key">
          <input
            id={id}
            className="mac-input"
            data-mono="true"
            type={shown ? 'text' : 'password'}
            autoComplete="off"
            spellCheck={false}
            placeholder={placeholder}
            value={text}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
          />
          <Button
            variant="ghost"
            size="icon"
            aria-label={
              shown ? t('settings.capabilities.key.hide') : t('settings.capabilities.key.show')
            }
            aria-pressed={shown}
            onClick={() => setShown((v) => !v)}
          >
            {shown ? (
              <EyeOff className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            ) : (
              <Eye className="size-3.5 text-muted-foreground" strokeWidth={1.75} aria-hidden />
            )}
          </Button>
        </span>
        {after}
      </Row>
    )
  }

  const numeric = field.type === 'int' || field.type === 'float'
  const listId = suggestions?.length ? `${id}-list` : undefined
  return (
    <Row label={label} help={helpText} htmlFor={id}>
      <input
        id={id}
        {...invalid}
        className="mac-input"
        data-mono={MONO.has(field.name) ? 'true' : undefined}
        data-short={numeric ? 'true' : undefined}
        data-wide={WIDE.has(field.name) ? 'true' : undefined}
        inputMode={numeric ? 'numeric' : undefined}
        autoComplete="off"
        spellCheck={false}
        list={listId}
        placeholder={placeholder ?? (field.placeholder || str(field.default)) ?? undefined}
        value={text}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
      {listId ? (
        <datalist id={listId}>
          {suggestions!.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
      ) : null}
      {after}
    </Row>
  )
}
