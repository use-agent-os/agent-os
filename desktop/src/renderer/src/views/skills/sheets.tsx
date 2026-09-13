import { useId, useState } from 'react'
import { ModalShell } from '@/components/ModalShell'
import type { RawSkill } from '@/views/skills/logic'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'

/**
 * Set a missing environment variable without leaving the panel: the operator
 * is already looking at why the skill is unavailable, and a trip to another
 * screen would lose that. The value is written through `env.set` and the
 * skill list is re-read, so the Missing card updates on save.
 */
export function SetEnvSheet({
  name,
  saving,
  onSubmit,
  onClose,
}: {
  name: string
  saving: boolean
  onSubmit: (value: string) => void
  onClose: () => void
}) {
  const titleId = useId()
  const inputId = useId()
  const [value, setValue] = useState('')
  return (
    <ModalShell
      role="dialog"
      labelledBy={titleId}
      onClose={onClose}
      dismissible={!saving}
      overlayClassName="sk-sheet__overlay"
      className="sk-sheet"
    >
      <form
        className="sk-sheet__form"
        onSubmit={(e) => {
          e.preventDefault()
          if (value) onSubmit(value)
        }}
      >
        <h2 id={titleId} className="sk-sheet__title">
          {t('skills.env.title')} <code>{name}</code>
        </h2>
        <p className="sk-sheet__body">{t('skills.env.body')}</p>
        <div className="mac-field">
          <label className="mac-label" htmlFor={inputId}>
            {t('skills.env.value')}
          </label>
          <input
            id={inputId}
            type="password"
            className="mac-input"
            autoComplete="off"
            autoFocus
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </div>
        <div className="sk-sheet__actions">
          <Button disabled={saving} onClick={onClose}>
            {t('skills.env.cancel')}
          </Button>
          <Button type="submit" variant="primary" disabled={saving || !value}>
            {saving ? t('skills.env.saving') : t('skills.env.save')}
          </Button>
        </div>
      </form>
    </ModalShell>
  )
}

/** Removal deletes files; a Mac app asks first. */
export function RemoveConfirm({
  skill,
  busy,
  onCancel,
  onConfirm,
}: {
  skill: RawSkill
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="sk-sheet__overlay"
      className="sk-alert"
    >
      <h2 id={titleId} className="sk-sheet__title">
        {t('skills.remove.title')}
      </h2>
      <p id={bodyId} className="sk-sheet__body">
        <strong>{String(skill.name || '')}</strong> — {t('skills.remove.body')}
      </p>
      <div className="sk-sheet__actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('skills.remove.cancel')}
        </Button>
        <Button variant="danger" disabled={busy} onClick={onConfirm}>
          {busy ? t('skills.action.removing') : t('skills.remove.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
