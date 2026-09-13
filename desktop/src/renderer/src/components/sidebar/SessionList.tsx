import { useMemo, useState, type DragEvent } from 'react'
import { filterSessions } from '@/views/sessions/logic'
import { t } from '~/i18n'
import { useLive } from '~/stores/live'
import { useMoveSession, useProjects } from '~/stores/projects'
import { useSessionMarks } from '~/stores/session-marks'
import { useSessionView } from '~/stores/session-view'
import { toSessionRow, useSessions } from '~/stores/sessions'
import { useUi } from '~/stores/ui'
import { fileSessions, SESSION_DRAG_TYPE } from '~/views/projects/logic'
import { agentIds, filterRows, hasActiveFilter, orderRows, sectionRows } from './logic'
import { ProjectFolders } from './ProjectFolders'
import { SessionRowLink } from './SessionRow'
import { SessionViewMenu } from './SessionViewMenu'

export { sessionPath } from './SessionRow'

/**
 * Project folders, then the loose sessions in the sections the view menu
 * asks for (by date unless told otherwise), pinned rows first. A session
 * filed in a project lives under its folder, not in the list; while a
 * search or a project filter is active every match shows flat so nothing
 * hides in a closed folder. Dragging a session onto the "Sessions" header
 * unfiles it.
 */
export function SessionList() {
  const query = useUi((s) => s.sessionQuery)
  const { rows, loading, error } = useSessions()
  const projectsState = useProjects()
  const { move } = useMoveSession()
  const view = useSessionView((s) => s.view)
  const setView = useSessionView((s) => s.set)
  const pinned = useSessionMarks((s) => s.pinned)
  const archived = useSessionMarks((s) => s.archived)
  const unread = useSessionMarks((s) => s.unread)
  const liveIds = useLive((s) => s.ids)
  const [over, setOver] = useState(false)
  const searching = Boolean(query.trim())
  const { projects } = projectsState

  // The filters first; the search narrows what they let through.
  const narrowed = useMemo(
    () => filterRows(rows, view, { archived }, liveIds),
    [rows, view, archived, liveIds],
  )
  const matched = useMemo(
    () =>
      searching
        ? filterSessions(
            narrowed.map((r) => r.raw),
            query,
          ).map(toSessionRow)
        : narrowed,
    [narrowed, searching, query],
  )

  const showFolders =
    view.folders && view.grouping !== 'project' && view.project === null && !searching
  const filed = useMemo(() => fileSessions(matched, projects), [matched, projects])

  const sections = useMemo(() => {
    // Folders hold their own rows; a pinned row shows on top even when filed.
    const loose = showFolders
      ? (() => {
          const unfiled = new Set(filed.unfiled.map((r) => r.key))
          return matched.filter((r) => unfiled.has(r.key) || pinned.has(r.key))
        })()
      : matched
    const ordered = orderRows(loose, view, { unread })
    return sectionRows(
      ordered,
      view,
      { pinned },
      {
        today: t('group.today'),
        yesterday: t('group.yesterday'),
        week: t('group.week'),
        pinned: t('view.section.pinned'),
        others: t('view.section.others'),
        noProject: t('view.section.noProject'),
        agent: (id) => id,
      },
      projects,
    )
  }, [showFolders, filed, matched, pinned, unread, view, projects])

  const agents = useMemo(() => agentIds(rows), [rows])
  const hidden = hasActiveFilter(view) ? rows.length - narrowed.length : 0

  function onDragOver(e: DragEvent) {
    if (!Array.from(e.dataTransfer.types).includes(SESSION_DRAG_TYPE)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (!over) setOver(true)
  }
  function onDrop(e: DragEvent) {
    if (!Array.from(e.dataTransfer.types).includes(SESSION_DRAG_TYPE)) return
    e.preventDefault()
    setOver(false)
    const key = e.dataTransfer.getData(SESSION_DRAG_TYPE)
    if (!key) return
    if (filed.unfiled.some((r) => r.key === key)) return
    move(key, null)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-2 pb-2">
      {showFolders ? (
        <ProjectFolders projects={projects} filed={filed} loading={projectsState.loading} />
      ) : null}
      <div
        className="mac-section proj-unfiled flex items-center justify-between"
        data-drop={over}
        onDragOver={onDragOver}
        onDragEnter={onDragOver}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <span>{over ? t('projects.unfiled.drop') : t('sidebar.sessions')}</span>
        <SessionViewMenu agents={agents} projects={projects} />
      </div>
      {loading ? (
        <p className="px-2.5 py-1 text-[11.5px] text-dim">{t('sidebar.sessions.loading')}</p>
      ) : null}
      {error ? <p className="px-2.5 py-1 text-[11.5px] text-danger">{error}</p> : null}
      {!loading && !error && rows.length === 0 ? (
        <p className="px-2.5 py-1 text-[11.5px] text-dim">{t('sidebar.sessions.empty')}</p>
      ) : null}
      {sections.map(({ key, label, items }) => (
        <div key={key}>
          {label ? <div className="mac-divider">{label}</div> : null}
          {items.map((row) => (
            <SessionRowLink key={row.key} row={row} />
          ))}
        </div>
      ))}
      {hidden > 0 ? (
        <p className="mac-list-hint">
          {hidden} {hidden === 1 ? t('view.hidden.one') : t('view.hidden.many')}
          <button
            type="button"
            className="app-no-drag"
            onClick={() => setView({ status: 'all', agent: null, project: null, archived: false })}
          >
            {t('view.hidden.clear')}
          </button>
        </p>
      ) : null}
    </div>
  )
}
