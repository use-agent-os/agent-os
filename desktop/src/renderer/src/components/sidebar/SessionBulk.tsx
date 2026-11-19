import { Archive, ArchiveRestore, Mail, MailOpen, Pin, PinOff, Trash2 } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { ModalShell } from '@/components/ModalShell'
import { MenuItem, MenuSep } from '~/components/menu/PopMenu'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useLive } from '~/stores/live'
import { useSessionMarks } from '~/stores/session-marks'
import { selectedRows, useSessionSelection } from '~/stores/session-selection'
import { isPinned, type SessionRow } from '~/stores/sessions'
import { usePinSessions, useRemoveSessions } from './session-actions'

/** Titles the delete alert names before it says "and N more". */
const NAMED = 3

/**
 * The context menu for a multi-row selection: what applies to every row at
 * once. Rename, Copy ID, Export and Move are one row's, so they are not here.
 * Pin, unread and archive flip all rows one way: on unless every row is on.
 * It follows the live selection, so a refetch that drops a row while the
 * menu is up also drops it from the count.
 */
export function SessionBulkMenuItems() {
  const marks = useSessionMarks()
  const visible = useSessionSelection((s) => s.visible)
  const selected = useSessionSelection((s) => s.keys)
  const askDelete = useSessionSelection((s) => s.askDelete)
  const rows = useMemo(() => selectedRows({ visible, keys: selected }), [visible, selected])
  const keys = rows.map((r) => r.key)
  const pin = usePinSessions()
  const allPinned = rows.every(isPinned)
  const allUnread = keys.every((k) => marks.unread.has(k))
  const allArchived = keys.every((k) => marks.archived.has(k))

  return (
    <>
      <MenuItem
        icon={allPinned ? PinOff : Pin}
        label={allPinned ? t('session.menu.unpin') : t('session.menu.pin')}
        onSelect={() => void pin(keys, !allPinned)}
      />
      <MenuItem
        icon={allUnread ? MailOpen : Mail}
        label={allUnread ? t('session.menu.markRead') : t('session.menu.markUnread')}
        onSelect={() => keys.forEach((k) => marks.setUnread(k, !allUnread))}
      />
      <MenuSep />
      <MenuItem
        icon={allArchived ? ArchiveRestore : Archive}
        label={allArchived ? t('session.menu.unarchive') : t('session.menu.archive')}
        onSelect={() => {
          keys.forEach((k) => marks.setArchived(k, !allArchived))
          const done = allArchived
            ? t('session.bulk.toast.unarchived')
            : t('session.bulk.toast.archived')
          toast.success(done.replace('{n}', String(keys.length)), { id: 'session-archive' })
        }}
      />
      <MenuItem
        icon={Trash2}
        tone="danger"
        label={t('session.bulk.menu.delete').replace('{n}', String(keys.length))}
        onSelect={() => askDelete(rows)}
      />
    </>
  )
}

/**
 * The batch delete alert, mounted once by the list rather than by the row
 * that was right-clicked: that row can move section (a running turn bumps
 * it to Today) and remount while the alert is up. What the gateway could
 * not delete stays selected, ready to retry.
 */
export function SessionBulkDelete() {
  const rows = useSessionSelection((s) => s.pendingDelete)
  const close = useSessionSelection((s) => s.closeDelete)
  const removeMany = useRemoveSessions()
  if (!rows) return null
  return (
    <DeleteSessionsConfirm
      rows={rows}
      onCancel={close}
      onConfirm={async () => {
        const { failed } = await removeMany(rows.map((r) => r.key))
        useSessionSelection.getState().select(failed)
        close()
      }}
    />
  )
}

/**
 * One alert for the whole batch: how many, the first few titles, and how
 * many of them are mid-turn (deleting cancels that turn).
 */
function DeleteSessionsConfirm({
  rows,
  onCancel,
  onConfirm,
}: {
  rows: readonly SessionRow[]
  onCancel: () => void
  onConfirm: () => Promise<void>
}) {
  const titleId = useId()
  const bodyId = useId()
  const [busy, setBusy] = useState(false)
  const liveIds = useLive((s) => s.ids)
  const running = rows.filter((r) => r.live || liveIds.has(r.key)).length
  const named = rows.slice(0, NAMED)
  const more = rows.length - named.length

  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      describedBy={bodyId}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="proj-alert__overlay"
      className="proj-alert"
    >
      <h2 id={titleId} className="proj-alert__title">
        {t('session.bulk.delete.title').replace('{n}', String(rows.length))}
      </h2>
      <div id={bodyId} className="proj-alert__body mac-bulk-body">
        <ul className="mac-bulk-titles">
          {named.map((r) => (
            <li key={r.key}>
              <strong>{r.title}</strong>
            </li>
          ))}
          {more > 0 ? <li>{t('session.bulk.delete.more').replace('{n}', String(more))}</li> : null}
        </ul>
        <p>{t('session.bulk.delete.body')}</p>
        {running > 0 ? (
          <p>
            {running === 1
              ? t('session.bulk.delete.runningOne')
              : t('session.bulk.delete.runningMany').replace('{n}', String(running))}
          </p>
        ) : null}
      </div>
      <div className="proj-alert__actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('session.delete.cancel')}
        </Button>
        <Button
          variant="danger"
          disabled={busy}
          onClick={() => {
            setBusy(true)
            void onConfirm().finally(() => setBusy(false))
          }}
        >
          {t('session.delete.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
