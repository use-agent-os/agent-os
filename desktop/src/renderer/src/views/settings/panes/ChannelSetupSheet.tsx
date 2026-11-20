import { ExternalLink, LoaderCircle, Lock } from 'lucide-react'
import { useId, useMemo, useRef, useState } from 'react'
import { ModalShell } from '@/components/ModalShell'
import type { MergedChannel } from '@/views/channels/logic'
import type { SettingsSnapshot } from '@/views/settings/snapshot'
import type { ChannelSpec, FieldSpec } from '@/views/setup/logic'
import { Button } from '~/components/ui/button'
import { Switch } from '~/components/ui/switch'
import { t, type MessageKey } from '~/i18n'
import { desktopApi } from '~/lib/desktop-api'
import { AdapterMark } from '../AdapterMark'
import {
  buildChannelEntry,
  configuredEntry,
  draftDirty,
  EMPTY_VALUE,
  isSecretField,
  seedDraft,
  splitFields,
  type ChannelDraft,
  type DraftValue,
} from '../channels-logic'
import { Notice } from '../parts'
import { keepFocus } from './focus'

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

function Field({
  field,
  draft,
  disabled,
  editing,
  onValue,
  onChecked,
}: {
  field: FieldSpec
  draft: DraftValue
  disabled: boolean
  /** An existing channel: a blank secret keeps the stored one. */
  editing: boolean
  onValue: (value: string) => void
  onChecked: (checked: boolean) => void
}) {
  const id = useId()
  const label = field.label || field.name
  const help = field.description || field.help
  const wide = field.name === 'name' || field.type === 'bool'

  if (field.type === 'bool') {
    return (
      <div className="mac-field chn-field" data-wide={wide ? 'true' : undefined}>
        <div className="chn-field__switch">
          <span>
            <span className="mac-label">{label}</span>
            {help ? <span className="mac-help">{help}</span> : null}
          </span>
          <Switch
            checked={draft.checked}
            disabled={disabled}
            aria-label={label}
            onCheckedChange={onChecked}
          />
        </div>
      </div>
    )
  }

  const secret = isSecretField(field)
  const control =
    field.type === 'select' ? (
      <select
        id={id}
        className="mac-select"
        value={draft.value}
        disabled={disabled}
        onChange={(e) => onValue(e.target.value)}
      >
        {(field.choices ?? []).map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    ) : (
      <input
        id={id}
        className="mac-input"
        data-mono={secret || field.name === 'name' ? 'true' : undefined}
        type={
          secret ? 'password' : field.type === 'int' || field.type === 'float' ? 'number' : 'text'
        }
        step={field.type === 'float' ? 'any' : undefined}
        autoComplete="off"
        spellCheck={false}
        placeholder={
          field.placeholder || (secret && editing ? t('settings.channels.sheet.secret.keep') : '')
        }
        value={draft.value}
        disabled={disabled}
        onChange={(e) => onValue(e.target.value)}
      />
    )

  return (
    <div className="mac-field chn-field" data-wide={wide ? 'true' : undefined}>
      <label className="mac-label" htmlFor={id}>
        {label}
        {field.required ? <span aria-hidden="true"> *</span> : null}
      </label>
      {control}
      {help ? <span className="mac-help">{help}</span> : null}
    </div>
  )
}

/**
 * Add or configure one channel: the console's channel setup, as a sheet in
 * the app's vocabulary. The adapter is picked once (locked on an edit), its
 * catalog fields are the form, and the entry goes to the owner's `onSave`,
 * which probes it with the gateway and writes it with the snapshot revision.
 * Secrets are write-only: a blank one on an edit keeps what is stored.
 */
export function ChannelSetupSheet({
  editingName,
  snapshot,
  loading,
  loadError,
  channels,
  saving,
  saveError,
  conflict,
  onRetry,
  onReload,
  onSave,
  onClose,
}: {
  editingName?: string
  snapshot?: SettingsSnapshot
  loading: boolean
  loadError?: string
  channels: readonly MergedChannel[]
  saving: boolean
  saveError?: string | null
  /** The gateway refused the last save because the file moved on. */
  conflict?: boolean
  onRetry: () => void
  onReload: () => void
  onSave: (entry: Record<string, unknown>) => void
  onClose: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  const specs: readonly ChannelSpec[] = snapshot?.catalog?.channels ?? []
  const entries = snapshot?.config?.channels?.channels
  const initialEntry = configuredEntry(entries, editingName)
  const initialType = String(
    initialEntry?.type || channels.find((c) => c.name === editingName)?.type || '',
  )
  const editing = Boolean(editingName)
  const initialSelected = specs.some((spec) => spec.type === initialType)
    ? initialType
    : editing
      ? ''
      : (specs[0]?.type ?? '')

  const [selected, setSelected] = useState(initialSelected)
  const [drafts, setDrafts] = useState<Record<string, ChannelDraft>>(() => {
    if (!initialSelected) return {}
    const spec = specs.find((s) => s.type === initialSelected)
    return {
      [initialSelected]: seedDraft(
        spec?.fields ?? [],
        initialSelected === initialType ? initialEntry : undefined,
      ),
    }
  })
  const [problem, setProblem] = useState<string | null>(null)
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  const spec = specs.find((s) => s.type === selected)
  const draft = useMemo(
    () => drafts[selected] ?? seedDraft(spec?.fields ?? []),
    [drafts, selected, spec],
  )
  const saved = useMemo(
    () => seedDraft(spec?.fields ?? [], selected === initialType ? initialEntry : undefined),
    [spec, selected, initialType, initialEntry],
  )
  const dirty = selected !== initialSelected || draftDirty(saved, draft)
  const { primary, advanced } = splitFields(spec, draft)
  const unsupportedEdit = editing && Boolean(initialType) && !spec
  const writeBlocked = Boolean(snapshot?.writeBlocked)

  function patch(name: string, value: Partial<DraftValue>) {
    setProblem(null)
    setDrafts((current) => ({
      ...current,
      [selected]: {
        ...(current[selected] ?? seedDraft(spec?.fields ?? [])),
        [name]: { ...(current[selected]?.[name] ?? EMPTY_VALUE), ...value },
      },
    }))
  }

  function pick(type: string) {
    if (editing || type === selected) return
    setProblem(null)
    setSelected(type)
    setDrafts((current) => {
      if (current[type]) return current
      const next = specs.find((s) => s.type === type)
      return { ...current, [type]: seedDraft(next?.fields ?? []) }
    })
  }

  function submit() {
    if (!spec) return
    const result = buildChannelEntry({ spec, draft, editingName, initialEntry, channels })
    if (!result.ok) {
      setProblem(
        result.error === 'missing'
          ? fill('settings.channels.sheet.required', { field: result.field })
          : fill('settings.channels.sheet.duplicate', { name: result.name }),
      )
      return
    }
    onSave(result.entry)
  }

  // Where focus was when the discard question opened. Making the form inert
  // drops that focus to <body>, outside this sheet, where Escape reaches no
  // handler; Keep editing puts it back.
  const beforeDiscard = useRef<HTMLElement | null>(null)
  const frameRef = useRef<HTMLDivElement>(null)

  function requestClose() {
    if (saving) return
    if (dirty) {
      beforeDiscard.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null
      setConfirmDiscard(true)
    } else onClose()
  }

  function keepEditing() {
    setConfirmDiscard(false)
    keepFocus(frameRef.current, () => beforeDiscard.current)
  }

  const title = editing
    ? fill('settings.channels.sheet.edit', { name: String(editingName) })
    : t('settings.channels.sheet.new')
  const feedback = writeBlocked
    ? { tone: 'danger' as const, text: t('settings.channels.sheet.writeBlocked') }
    : conflict
      ? { tone: 'warn' as const, text: t('settings.channels.sheet.conflict'), reload: true }
      : problem || saveError
        ? { tone: 'danger' as const, text: problem || saveError || '' }
        : null
  const canSave =
    !saving && !loading && !loadError && Boolean(spec) && !writeBlocked && !conflict && dirty

  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={requestClose}
      dismissible={!saving}
      overlayClassName="chn-sheet__overlay"
      className="chn-sheet"
    >
      <div
        ref={frameRef}
        className="chn-sheet__frame"
        inert={confirmDiscard ? true : undefined}
        aria-hidden={confirmDiscard ? true : undefined}
      >
        <header className="chn-sheet__head">
          <AdapterMark type={selected || initialType} size={36} />
          <div>
            <h2 id={titleId}>{title}</h2>
            <p id={bodyId}>
              {editing
                ? t('settings.channels.sheet.edit.blurb')
                : t('settings.channels.sheet.new.blurb')}
            </p>
          </div>
        </header>

        <div className="chn-sheet__body">
          {loading ? (
            <div className="chn-sheet__state" role="status">
              <LoaderCircle className="stg-spin size-3.5" strokeWidth={1.75} aria-hidden />
              {t('settings.channels.sheet.loading')}
            </div>
          ) : loadError ? (
            <Notice
              tone="danger"
              action={<Button onClick={onRetry}>{t('settings.channels.retry')}</Button>}
            >
              {`${t('settings.channels.sheet.loadFailed')} ${loadError}`}
            </Notice>
          ) : specs.length === 0 ? (
            <Notice tone="info">{t('settings.channels.sheet.noAdapters')}</Notice>
          ) : unsupportedEdit ? (
            <Notice tone="info">
              {fill('settings.channels.sheet.noGuided', { type: initialType })}
            </Notice>
          ) : (
            <>
              <section className="chn-section" aria-label={t('settings.channels.sheet.adapter')}>
                <div className="chn-section__title">
                  <span>{t('settings.channels.sheet.adapter')}</span>
                  {editing ? (
                    <span className="mac-help">{t('settings.channels.sheet.adapter.locked')}</span>
                  ) : null}
                </div>
                <div
                  className="chn-types"
                  role="radiogroup"
                  aria-label={t('settings.channels.sheet.adapter')}
                >
                  {specs.map((candidate) => (
                    <button
                      key={candidate.type}
                      type="button"
                      role="radio"
                      aria-checked={selected === candidate.type}
                      className="chn-type app-no-drag"
                      disabled={saving || (editing && selected !== candidate.type)}
                      onClick={() => pick(candidate.type)}
                    >
                      <AdapterMark type={candidate.type} size={30} />
                      <span className="chn-type__text">
                        <span className="chn-type__name">{candidate.label || candidate.type}</span>
                        <span className="chn-type__meta">
                          {candidate.transport || t('settings.channels.sheet.transport.unknown')}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              </section>

              {spec ? (
                <section className="chn-section" aria-label={t('settings.channels.sheet.details')}>
                  <div className="chn-section__title">
                    <span>{t('settings.channels.sheet.details')}</span>
                    {spec.docsHint ? (
                      <button
                        type="button"
                        className="prov-keylink"
                        onClick={() => void desktopApi().app.openExternal(String(spec.docsHint))}
                      >
                        {t('settings.channels.sheet.guide')}
                        <ExternalLink className="size-3" strokeWidth={2} aria-hidden />
                      </button>
                    ) : null}
                  </div>
                  {spec.description || spec.help ? (
                    <p className="chn-section__blurb">{spec.description || spec.help}</p>
                  ) : null}
                  {spec.whatYouNeed?.length ? (
                    <div className="chn-needs">
                      <span className="chn-needs__title">{t('settings.channels.sheet.needs')}</span>
                      <ul>
                        {spec.whatYouNeed.map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                  <div className="chn-fields">
                    {primary.map((field) => (
                      <Field
                        key={field.name}
                        field={field}
                        draft={draft[field.name] ?? EMPTY_VALUE}
                        disabled={saving || (editing && field.name === 'name')}
                        editing={editing}
                        onValue={(value) => patch(field.name, { value })}
                        onChecked={(checked) => patch(field.name, { checked })}
                      />
                    ))}
                  </div>
                  {advanced.length ? (
                    <details className="stg-details chn-advanced">
                      <summary className="stg-details__summary chn-advanced__summary">
                        <span>{t('settings.channels.sheet.advanced')}</span>
                      </summary>
                      <div className="chn-fields">
                        {advanced.map((field) => (
                          <Field
                            key={field.name}
                            field={field}
                            draft={draft[field.name] ?? EMPTY_VALUE}
                            disabled={saving}
                            editing={editing}
                            onValue={(value) => patch(field.name, { value })}
                            onChecked={(checked) => patch(field.name, { checked })}
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

        <footer className="chn-sheet__foot">
          <div className="chn-sheet__feedback" aria-live="polite">
            {feedback ? (
              <span className="chn-sheet__problem" data-tone={feedback.tone} role="alert">
                {feedback.text}
                {feedback.reload ? (
                  <Button onClick={onReload}>{t('settings.channels.sheet.reload')}</Button>
                ) : null}
              </span>
            ) : (
              <span className="chn-sheet__note">
                <Lock className="size-3" strokeWidth={2} aria-hidden />
                {t('settings.channels.sheet.secretsNote')}
              </span>
            )}
          </div>
          <div className="chn-sheet__buttons">
            <Button disabled={saving} onClick={requestClose}>
              {t('settings.channels.cancel')}
            </Button>
            <Button variant="primary" disabled={!canSave} onClick={submit}>
              {saving ? (
                <>
                  <LoaderCircle className="stg-spin size-3.5" strokeWidth={2} aria-hidden />
                  {t('settings.channels.sheet.saving')}
                </>
              ) : editing ? (
                t('settings.channels.sheet.update')
              ) : (
                t('settings.channels.sheet.save')
              )}
            </Button>
          </div>
        </footer>
      </div>

      {confirmDiscard ? (
        <DiscardDialog
          onKeep={keepEditing}
          onDiscard={() => {
            setConfirmDiscard(false)
            onClose()
          }}
        />
      ) : null}
    </ModalShell>
  )
}

function DiscardDialog({ onKeep, onDiscard }: { onKeep: () => void; onDiscard: () => void }) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onKeep}
      overlayClassName="stg-confirm__overlay"
      className="stg-confirm"
    >
      <h2 id={titleId}>{t('settings.channels.sheet.discard.title')}</h2>
      <p id={bodyId}>{t('settings.channels.sheet.discard.body')}</p>
      <div className="stg-confirm__actions">
        <Button onClick={onKeep}>{t('settings.channels.sheet.discard.keep')}</Button>
        <Button variant="danger" onClick={onDiscard}>
          {t('settings.channels.sheet.discard.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
