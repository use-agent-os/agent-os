import { useEffect, useMemo, useState } from 'react'
import { AnimatePresence } from 'motion/react'
import {
  CheckIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  LoaderCircleIcon,
  LockKeyholeIcon,
  RadioTowerIcon,
  ShieldCheckIcon,
  XIcon,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ModalShell } from '@/components/ModalShell'
import {
  readScopedFields,
  validateScopedRequiredFields,
  type FieldSpec,
  type ScopedField,
} from '@/views/setup/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import { AdapterLogo } from './AdapterLogo'
import type { MergedChannel } from './logic'

type DraftValue = { value: string; checked: boolean }
type Draft = Record<string, DraftValue>

function seedDraft(fields: FieldSpec[], initial?: Record<string, unknown>): Draft {
  return Object.fromEntries(
    fields.map((field) => {
      const isSecret = Boolean(field.secret || field.type === 'password')
      const configured = isSecret ? undefined : initial?.[field.name]
      const seed = configured ?? field.default
      return [
        field.name,
        {
          value: field.type === 'bool' ? '' : String(seed ?? ''),
          checked: field.type === 'bool' ? Boolean(seed) : false,
        },
      ]
    }),
  )
}

function fieldIsVisible(field: FieldSpec, draft: Draft): boolean {
  const conditions = field.showWhen
  if (!conditions || Object.keys(conditions).length === 0) return true
  return Object.entries(conditions).every(([name, expected]) => {
    const actual = draft[name]
    return typeof expected === 'boolean'
      ? actual?.checked === expected
      : String(actual?.value ?? '') === String(expected ?? '')
  })
}

function ChannelField({
  field,
  draft,
  disabled,
  onValue,
  onChecked,
}: {
  field: FieldSpec
  draft: DraftValue
  disabled: boolean
  onValue: (value: string) => void
  onChecked: (checked: boolean) => void
}) {
  const label = field.label || field.name
  const required = field.required ? <span aria-hidden="true"> *</span> : null
  const helper = field.description ? <small>{field.description}</small> : null
  const wide = field.name === 'enabled'

  if (field.type === 'bool') {
    return (
      <label
        className={`ch-setup__field ch-setup__field--check${wide ? ' is-wide' : ''}`}
        data-field={field.name}
      >
        <span>
          {label}
          {required}
        </span>
        {helper}
        <span className={`ch-setup__check${disabled ? ' is-disabled' : ''}`}>
          <input
            type="checkbox"
            checked={draft.checked}
            disabled={disabled}
            aria-label={label}
            onChange={(event) => onChecked(event.target.checked)}
          />
          <span className="ch-setup__check-control" aria-hidden="true">
            <CheckIcon />
          </span>
          <span>
            <strong>{draft.checked ? 'On' : 'Off'}</strong>
          </span>
        </span>
      </label>
    )
  }

  if (field.type === 'select') {
    return (
      <label className={`ch-setup__field${wide ? ' is-wide' : ''}`} data-field={field.name}>
        <span>
          {label}
          {required}
        </span>
        {helper}
        <span className="ch-setup__select">
          <select
            aria-label={label}
            value={draft.value}
            disabled={disabled}
            required={field.required}
            onChange={(event) => onValue(event.target.value)}
          >
            {(field.choices || []).map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
          <ChevronDownIcon aria-hidden="true" />
        </span>
      </label>
    )
  }

  const secret = Boolean(field.secret || field.type === 'password')
  const inputType = secret
    ? 'password'
    : field.type === 'int' || field.type === 'float'
      ? 'number'
      : 'text'
  return (
    <label className={`ch-setup__field${wide ? ' is-wide' : ''}`} data-field={field.name}>
      <span>
        {label}
        {required}
      </span>
      {helper}
      <span className={`ch-setup__input${secret ? ' is-secret' : ''}`}>
        {secret ? <LockKeyholeIcon aria-hidden="true" /> : null}
        <input
          type={inputType}
          aria-label={label}
          value={draft.value}
          disabled={disabled}
          required={field.required}
          placeholder={
            field.placeholder || (secret ? 'Leave blank to keep the current credential' : '')
          }
          step={field.type === 'float' ? 'any' : undefined}
          onChange={(event) => onValue(event.target.value)}
        />
      </span>
    </label>
  )
}

export function ChannelSetupDialog({
  open,
  editingName,
  snapshot,
  runtimeChannels,
  loading,
  loadError,
  saving,
  saveError,
  onRetry,
  onSave,
  onClose,
  onDiscard,
  navigationBlocked,
  onKeepNavigation,
  onDiscardNavigation,
  onResolveConflict,
  onOpenAdvanced,
  onDirtyChange,
}: {
  open: boolean
  editingName?: string
  snapshot?: SettingsSnapshot
  runtimeChannels: MergedChannel[]
  loading: boolean
  loadError?: string
  saving: boolean
  saveError?: string
  onRetry: () => void
  onSave: (entry: Record<string, unknown>, expectedRevision?: string) => void
  onClose: () => void
  onDiscard: () => void
  navigationBlocked: boolean
  onKeepNavigation: () => void
  onDiscardNavigation: () => void
  onResolveConflict: () => void
  onOpenAdvanced: () => void
  onDirtyChange: (dirty: boolean) => void
}) {
  const specs = snapshot?.catalog?.channels ?? []
  const configuredEntries = snapshot?.config?.channels?.channels ?? []
  const initialEntry = editingName
    ? configuredEntries.find((entry) => String(entry.name || '') === editingName)
    : undefined
  const initialType = String(
    initialEntry?.type || runtimeChannels.find((c) => c.name === editingName)?.type || '',
  )
  const initialSelected = specs.some((spec) => spec.type === initialType)
    ? initialType
    : editingName
      ? ''
      : specs[0]?.type || ''
  const initialSpec = specs.find((spec) => spec.type === initialSelected)

  const [selected, setSelected] = useState(initialSelected)
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() =>
    initialSelected
      ? {
          [initialSelected]: seedDraft(
            initialSpec?.fields || [],
            initialSelected === initialType ? initialEntry : undefined,
          ),
        }
      : {},
  )
  const [dirty, setDirty] = useState(false)
  const [baseRevision, setBaseRevision] = useState<string>()
  const [validationMessage, setValidationMessage] = useState('')
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])

  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const spec = specs.find((candidate) => candidate.type === selected)
  const draft = drafts[selected] ?? seedDraft(spec?.fields || [])
  const visibleFields = useMemo(
    () => (spec?.fields || []).filter((field) => fieldIsVisible(field, draft)),
    [draft, spec?.fields],
  )
  const primaryFields = visibleFields.filter((field) => !field.advanced)
  const advancedFields = visibleFields.filter((field) => field.advanced)
  const isEditing = Boolean(editingName)
  const unsupportedEdit = Boolean(isEditing && initialType && !spec)
  const canKeepSecret = Boolean(initialEntry && String(initialEntry.type || '') === selected)
  const snapshotRevision = typeof snapshot?.revision === 'string' ? snapshot.revision : undefined
  const writeBlocked =
    snapshot?.writeBlocked === true ||
    snapshot?.diskDiverged === true ||
    snapshot?.revision === null
  const conflicted = Boolean(
    dirty && baseRevision && snapshotRevision && baseRevision !== snapshotRevision,
  )

  const markDirty = () => {
    if (!dirty) {
      setDirty(true)
      setBaseRevision(snapshotRevision)
    }
    setValidationMessage('')
  }

  const setValue = (name: string, value: string) => {
    markDirty()
    setDrafts((current) => ({
      ...current,
      [selected]: {
        ...(current[selected] ?? seedDraft(spec?.fields || [])),
        [name]: { ...(current[selected]?.[name] ?? { value: '', checked: false }), value },
      },
    }))
  }

  const setChecked = (name: string, checked: boolean) => {
    markDirty()
    setDrafts((current) => ({
      ...current,
      [selected]: {
        ...(current[selected] ?? seedDraft(spec?.fields || [])),
        [name]: { ...(current[selected]?.[name] ?? { value: '', checked: false }), checked },
      },
    }))
  }

  const selectType = (type: string) => {
    if (type === selected || isEditing) return
    markDirty()
    setSelected(type)
    setDrafts((current) => {
      if (current[type]) return current
      const nextSpec = specs.find((candidate) => candidate.type === type)
      return { ...current, [type]: seedDraft(nextSpec?.fields || []) }
    })
  }

  const collectAndSave = () => {
    if (!spec) return
    const fields: ScopedField[] = visibleFields.map((field) => ({
      name: field.name,
      value: draft[field.name]?.value ?? '',
      checked: draft[field.name]?.checked ?? false,
      type:
        field.type === 'bool'
          ? 'checkbox'
          : field.secret || field.type === 'password'
            ? 'password'
            : 'text',
      secret: Boolean(field.secret || field.type === 'password'),
      required: Boolean(field.required),
      hidden: false,
      label: field.label,
    }))
    const missing = validateScopedRequiredFields(fields, canKeepSecret)
    if (missing) {
      setValidationMessage(`${missing} is required.`)
      return
    }
    const secretNames = new Set(
      (spec.fields || [])
        .filter((field) => field.secret || field.type === 'password')
        .map((field) => field.name),
    )
    const preservedEntry =
      isEditing && initialEntry
        ? Object.fromEntries(
            Object.entries(initialEntry).filter(([name]) => !secretNames.has(name)),
          )
        : {}
    const entry: Record<string, unknown> = {
      ...preservedEntry,
      type: selected,
      ...readScopedFields(fields, 'channel'),
    }
    const name = String(entry.name || '').trim()
    const duplicate = runtimeChannels.find(
      (channel) => String(channel.name || '') === name && channel.name !== editingName,
    )
    if (duplicate) {
      setValidationMessage(`A channel named “${name}” already exists.`)
      return
    }
    setConfirmDiscard(false)
    onSave(entry, baseRevision ?? snapshotRevision)
  }

  const requestClose = () => {
    if (saving) return
    if (dirty) {
      setConfirmDiscard(true)
      return
    }
    onClose()
  }

  const resolveConflict = () => {
    if (!snapshotRevision) return
    setBaseRevision(snapshotRevision)
    setValidationMessage('')
    onResolveConflict()
  }

  const confirmationOpen = confirmDiscard || navigationBlocked
  const phase = saving ? 3 : spec ? 2 : 1
  const panelTitle = isEditing ? `Configure ${editingName}` : 'Add a channel'

  return (
    <AnimatePresence>
      {open ? (
        <ModalShell
          role="dialog"
          labelledBy="channel-setup-title"
          describedBy="channel-setup-description"
          onClose={requestClose}
          overlayClassName="ch-setup-overlay"
          className="ch-setup-dialog"
        >
          <div
            className="ch-setup__content"
            inert={confirmationOpen ? true : undefined}
            aria-hidden={confirmationOpen ? true : undefined}
          >
            <header className="ch-setup__header">
              <span className="ch-setup__header-icon" aria-hidden="true">
                <RadioTowerIcon />
              </span>
              <div>
                <span className="t-label">
                  {isEditing ? 'Channel configuration' : 'New integration'}
                </span>
                <h2 id="channel-setup-title">{panelTitle}</h2>
                <p id="channel-setup-description">
                  {isEditing
                    ? 'Update this adapter without exposing its saved credentials.'
                    : 'Choose an adapter, add its credentials, then validate and save it.'}
                </p>
              </div>
              <button
                type="button"
                className="ch-setup__close"
                aria-label="Close channel setup"
                disabled={saving}
                onClick={requestClose}
              >
                <XIcon />
              </button>
            </header>

            <ol className="ch-setup__flow" aria-label="Channel setup progress">
              {['Choose adapter', 'Enter details', 'Validate & save'].map((label, index) => {
                const step = index + 1
                return (
                  <li
                    key={label}
                    className={`${step < phase ? 'is-done' : ''}${step === phase ? ' is-active' : ''}`}
                    aria-current={step === phase ? 'step' : undefined}
                  >
                    <span>{step < phase ? <CheckIcon /> : step}</span>
                    <strong>{label}</strong>
                  </li>
                )
              })}
            </ol>

            <div className="ch-setup__body">
              {loading ? (
                <div className="ch-setup__state" role="status">
                  <LoaderCircleIcon className="ch-refresh-spin" />
                  <strong>Loading channel options…</strong>
                  <span>Reading the current configuration and adapter catalog.</span>
                </div>
              ) : loadError ? (
                <div className="ch-setup__state is-error" role="alert">
                  <strong>Channel setup could not be loaded.</strong>
                  <span>{loadError}</span>
                  <Button type="button" variant="outline" onClick={onRetry}>
                    Try again
                  </Button>
                </div>
              ) : specs.length === 0 ? (
                <div className="ch-setup__state" role="status">
                  <strong>No channel adapters are available.</strong>
                  <span>Check the gateway catalog, then refresh this page.</span>
                </div>
              ) : unsupportedEdit ? (
                <div className="ch-setup__state" role="status">
                  <strong>This adapter needs Advanced config.</strong>
                  <span>
                    {initialType} is running, but it is not available in the guided channel catalog.
                  </span>
                  <Button type="button" variant="outline" onClick={onOpenAdvanced}>
                    Open Advanced config
                  </Button>
                </div>
              ) : (
                <>
                  <section className="ch-setup__section" aria-labelledby="channel-adapter-heading">
                    <div className="ch-setup__section-head">
                      <div>
                        <span className="t-label">Step 1</span>
                        <h3 id="channel-adapter-heading">Choose an adapter</h3>
                      </div>
                      {isEditing ? (
                        <span className="ch-setup__locked">Type locked while editing</span>
                      ) : null}
                    </div>
                    <div className="ch-setup__types" role="radiogroup" aria-label="Channel adapter">
                      {specs.map((candidate) => (
                        <button
                          key={candidate.type}
                          type="button"
                          role="radio"
                          aria-checked={selected === candidate.type}
                          disabled={isEditing && selected !== candidate.type}
                          className={selected === candidate.type ? 'is-selected' : ''}
                          onClick={() => selectType(candidate.type)}
                        >
                          <span className="ch-setup__type-mark" aria-hidden="true">
                            <AdapterLogo type={candidate.type} />
                          </span>
                          <span>
                            <strong>{candidate.label || candidate.type}</strong>
                            <small>{candidate.transport || 'messaging adapter'}</small>
                          </span>
                          <span className="ch-setup__radio-dot" aria-hidden="true" />
                        </button>
                      ))}
                    </div>
                  </section>

                  {spec ? (
                    <section
                      className="ch-setup__section"
                      aria-labelledby="channel-details-heading"
                    >
                      <div className="ch-setup__section-head">
                        <div>
                          <span className="t-label">Step 2</span>
                          <h3 id="channel-details-heading">{spec.label || spec.type} details</h3>
                        </div>
                        {spec.docsHint ? (
                          <a href={spec.docsHint} target="_blank" rel="noreferrer">
                            Setup guide
                            <ExternalLinkIcon aria-hidden="true" />
                          </a>
                        ) : null}
                      </div>
                      {spec.description || spec.help ? (
                        <p className="ch-setup__adapter-copy">{spec.description || spec.help}</p>
                      ) : null}
                      {spec.whatYouNeed?.length ? (
                        <div className="ch-setup__needs">
                          <ShieldCheckIcon aria-hidden="true" />
                          <div>
                            <strong>Before you start</strong>
                            <ul>
                              {spec.whatYouNeed.map((item) => (
                                <li key={item}>{item}</li>
                              ))}
                            </ul>
                          </div>
                        </div>
                      ) : null}

                      <div className="ch-setup__fields">
                        {primaryFields.map((field) => (
                          <ChannelField
                            key={field.name}
                            field={field}
                            draft={draft[field.name] ?? { value: '', checked: false }}
                            disabled={saving || (isEditing && field.name === 'name')}
                            onValue={(value) => setValue(field.name, value)}
                            onChecked={(checked) => setChecked(field.name, checked)}
                          />
                        ))}
                      </div>

                      {advancedFields.length ? (
                        <details className="ch-setup__advanced">
                          <summary>Advanced options</summary>
                          <div className="ch-setup__fields">
                            {advancedFields.map((field) => (
                              <ChannelField
                                key={field.name}
                                field={field}
                                draft={draft[field.name] ?? { value: '', checked: false }}
                                disabled={saving}
                                onValue={(value) => setValue(field.name, value)}
                                onChecked={(checked) => setChecked(field.name, checked)}
                              />
                            ))}
                          </div>
                        </details>
                      ) : null}
                    </section>
                  ) : null}
                </>
              )}
            </div>

            <footer className="ch-setup__footer">
              <div className="ch-setup__feedback" aria-live="polite">
                {writeBlocked ? (
                  <span className="is-error">
                    Configuration changed on disk. Reload the gateway state before saving.
                  </span>
                ) : conflicted ? (
                  <div className="ch-setup__conflict">
                    <span className="is-error">
                      Settings changed elsewhere. Your channel draft is still preserved.
                    </span>
                    <Button type="button" size="sm" variant="outline" onClick={resolveConflict}>
                      Use latest version
                    </Button>
                  </div>
                ) : validationMessage || saveError ? (
                  <span className="is-error">{validationMessage || saveError}</span>
                ) : (
                  <span>
                    <LockKeyholeIcon aria-hidden="true" />
                    Credentials are write-only and never shown again.
                  </span>
                )}
              </div>
              <div className="ch-setup__footer-actions">
                <Button type="button" variant="outline" disabled={saving} onClick={requestClose}>
                  Cancel
                </Button>
                <Button
                  type="button"
                  disabled={
                    saving || loading || Boolean(loadError) || !spec || writeBlocked || conflicted
                  }
                  onClick={collectAndSave}
                >
                  {saving ? <LoaderCircleIcon className="ch-refresh-spin" /> : <ShieldCheckIcon />}
                  <span>
                    {saving ? 'Validating…' : isEditing ? 'Validate & update' : 'Validate & add'}
                  </span>
                </Button>
              </div>
            </footer>
          </div>

          {confirmationOpen ? (
            <div
              className="ch-setup__discard"
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="discard-channel-title"
              onKeyDown={(event) => {
                if (event.key !== 'Escape') return
                event.stopPropagation()
                if (saving) return
                if (navigationBlocked) onKeepNavigation()
                else setConfirmDiscard(false)
              }}
            >
              <div>
                <strong id="discard-channel-title">Discard this channel draft?</strong>
                <span>
                  {navigationBlocked
                    ? 'Leaving this page will clear unsaved credentials and field changes.'
                    : 'Your unsaved credentials and field changes will be cleared.'}
                </span>
              </div>
              <div>
                <Button
                  type="button"
                  variant="outline"
                  autoFocus
                  disabled={saving}
                  onClick={() => {
                    if (navigationBlocked) onKeepNavigation()
                    else setConfirmDiscard(false)
                  }}
                >
                  Keep editing
                </Button>
                <Button
                  type="button"
                  variant="destructive"
                  disabled={saving}
                  onClick={navigationBlocked ? onDiscardNavigation : onDiscard}
                >
                  Discard draft
                </Button>
              </div>
            </div>
          ) : null}
        </ModalShell>
      ) : null}
    </AnimatePresence>
  )
}
