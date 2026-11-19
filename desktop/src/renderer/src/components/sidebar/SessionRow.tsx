import { Archive, MoreHorizontal, Pin } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { NavLink } from 'react-router'
import { ModalShell } from '@/components/ModalShell'
import { PopMenu, type MenuPlace } from '~/components/menu/PopMenu'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { shortAge } from '~/lib/relative-time'
import { useLive } from '~/stores/live'
import { useSessionMarks } from '~/stores/session-marks'
import { useSessionSelection } from '~/stores/session-selection'
import { useSessionView } from '~/stores/session-view'
import { isPinned, type SessionRow } from '~/stores/sessions'
import { SESSION_DRAG_TYPE } from '~/views/projects/logic'
import { useSessionActions } from './session-actions'
import { SessionBulkMenuItems } from './SessionBulk'
import { SessionMenuItems } from './SessionMenu'

/** Route path for a session; keys carry colons, so they are encoded once. */
export function sessionPath(key: string): string {
  return `/sessions/${encodeURIComponent(key)}`
}

/**
 * One session row. Draggable so it can be filed into a project folder;
 * right-click (or the "…" that appears on hover) opens its menu; "Rename…"
 * turns the title into a field in place. The dot at the left is the state:
 * a pin, an archive box, a breathing light while a turn runs. Cmd-click and
 * Shift-click select rows instead of opening them; right-clicking inside a
 * selection of several opens the menu for all of them.
 */
export function SessionRowLink({ row, nested = false }: { row: SessionRow; nested?: boolean }) {
  const liveLocally = useLive((s) => s.ids.has(row.key))
  const live = row.live || liveLocally
  const pinned = isPinned(row)
  const archived = useSessionMarks((s) => s.archived.has(row.key))
  const unread = useSessionMarks((s) => s.unread.has(row.key))
  const showAge = useSessionView((s) => s.view.ages)
  const actions = useSessionActions(row)
  const selected = useSessionSelection((s) => s.keys.has(row.key))
  const several = useSessionSelection((s) => s.keys.size > 1)
  const bulk = selected && several

  const [menu, setMenu] = useState<MenuPlace | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const moreRef = useRef<HTMLButtonElement>(null)
  const linkRef = useRef<HTMLAnchorElement>(null)
  const closeMenu = useCallback(() => setMenu(null), [])

  function openMenu(place: MenuPlace) {
    // Finder: a right-click outside the selection makes this row the selection.
    const selection = useSessionSelection.getState()
    if (selection.keys.size > 0 && !selection.keys.has(row.key)) selection.select([row.key])
    setMenu(place)
  }

  function openAtRow() {
    const rect = linkRef.current?.getBoundingClientRect()
    if (rect) openMenu({ anchor: rect, align: 'start' })
  }

  function onKeyDown(e: KeyboardEvent<HTMLAnchorElement>) {
    const selection = useSessionSelection.getState()
    if (e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey)) {
      e.preventDefault()
      openAtRow()
    } else if (e.key === 'Escape' && selection.keys.size > 0) {
      e.preventDefault()
      selection.clear()
    } else if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'a') {
      e.preventDefault()
      selection.selectAll()
    }
  }

  const stateGlyph = live ? (
    <span className="mac-session-dot" data-live="true" aria-hidden />
  ) : pinned ? (
    <Pin className="mac-session-glyph" strokeWidth={2} aria-label={t('session.pinned')} />
  ) : archived ? (
    <Archive className="mac-session-glyph" strokeWidth={2} aria-label={t('session.archived')} />
  ) : (
    <span className="mac-session-dot" data-unread={unread} aria-hidden />
  )

  return (
    <div
      className="mac-session-item"
      data-menu={menu !== null}
      data-renaming={renaming}
      onContextMenu={(e) => {
        if (renaming) return
        e.preventDefault()
        openMenu({ at: { x: e.clientX, y: e.clientY } })
      }}
    >
      {renaming ? (
        <div className="mac-session" data-nested={nested} data-live={live}>
          {stateGlyph}
          <RenameField
            value={row.title}
            label={t('session.rename.label')}
            onDone={(next) => {
              setRenaming(false)
              if (next !== null) void actions.rename(next)
            }}
          />
        </div>
      ) : (
        <NavLink
          ref={linkRef}
          to={sessionPath(row.key)}
          className="mac-session app-no-drag"
          data-nested={nested}
          data-live={live}
          data-unread={unread}
          data-archived={archived}
          data-selected={selected}
          title={row.title}
          draggable
          onKeyDown={onKeyDown}
          onClick={(e) => {
            // Cmd and Shift select; without them the click opens the chat as
            // always, and clears the selection.
            const selection = useSessionSelection.getState()
            if (e.metaKey) {
              e.preventDefault()
              selection.toggle(row.key, nested)
            } else if (e.shiftKey) {
              e.preventDefault()
              selection.extend(row.key, nested)
            } else {
              selection.click(row.key, nested)
            }
          }}
          onDragStart={(e) => {
            e.dataTransfer.setData(SESSION_DRAG_TYPE, row.key)
            e.dataTransfer.setData('text/plain', row.title)
            e.dataTransfer.effectAllowed = 'move'
          }}
        >
          {stateGlyph}
          <span className="mac-session-title">{row.title}</span>
          {showAge ? (
            <span className="mac-session-age">{row.updatedAt ? shortAge(row.updatedAt) : ''}</span>
          ) : (
            <span />
          )}
        </NavLink>
      )}
      {renaming ? null : (
        <button
          ref={moreRef}
          type="button"
          className="mac-session-more app-no-drag"
          aria-label={t('session.more')}
          aria-haspopup="menu"
          aria-expanded={menu !== null}
          tabIndex={-1}
          onClick={(e) => {
            e.stopPropagation()
            const rect = e.currentTarget.getBoundingClientRect()
            if (menu) setMenu(null)
            else openMenu({ anchor: rect, align: 'end' })
          }}
        >
          <MoreHorizontal className="size-3.5" strokeWidth={2} aria-hidden />
        </button>
      )}
      {menu ? (
        <PopMenu
          place={menu}
          onClose={closeMenu}
          label={bulk ? t('session.bulk.menu.label') : t('session.menu.label')}
          triggerRef={moreRef}
        >
          {bulk ? (
            <SessionBulkMenuItems />
          ) : (
            <SessionMenuItems
              row={row}
              actions={actions}
              onRename={() => setRenaming(true)}
              onDelete={() => setConfirmDelete(true)}
            />
          )}
        </PopMenu>
      ) : null}
      {confirmDelete ? (
        <DeleteSessionConfirm
          name={row.title}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={async () => {
            const ok = await actions.remove()
            if (!ok) setConfirmDelete(false)
          }}
        />
      ) : null}
    </div>
  )
}

/**
 * The title as a field: Return commits, Escape reverts, leaving the field
 * commits too (Finder's rename). Selected on open so typing replaces.
 * Project folders rename through it as well. `via` says how it ended: from
 * a key, focus is still in the field; from a blur, it is already on its way
 * to wherever the user sent it.
 */
export function RenameField({
  value,
  label,
  onDone,
}: {
  value: string
  label: string
  onDone: (next: string | null, via: 'key' | 'blur') => void
}) {
  const [draft, setDraft] = useState(value)
  const ref = useRef<HTMLInputElement>(null)
  const settled = useRef(false)
  useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])
  const finish = (next: string | null, via: 'key' | 'blur') => {
    if (settled.current) return
    settled.current = true
    onDone(next, via)
  }
  return (
    <input
      ref={ref}
      className="mac-session-rename app-no-drag"
      value={draft}
      aria-label={label}
      maxLength={200}
      spellCheck={false}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          finish(draft, 'key')
        } else if (e.key === 'Escape') {
          e.preventDefault()
          e.stopPropagation()
          finish(null, 'key')
        }
      }}
      onBlur={() => finish(draft, 'blur')}
      onClick={(e) => e.stopPropagation()}
    />
  )
}

function DeleteSessionConfirm({
  name,
  onCancel,
  onConfirm,
}: {
  name: string
  onCancel: () => void
  onConfirm: () => Promise<void>
}) {
  const titleId = useId()
  const bodyId = useId()
  const [busy, setBusy] = useState(false)
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
        {t('session.delete.title')}
      </h2>
      <p id={bodyId} className="proj-alert__body">
        <strong>{name}</strong> — {t('session.delete.body')}
      </p>
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
