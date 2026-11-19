import { useEffect, useMemo, useState, type DragEvent } from 'react'
import { projectId } from '@/views/projects/logic'
import { filterSessions } from '@/views/sessions/logic'
import { t } from '~/i18n'
import { useLive } from '~/stores/live'
import { useMoveSession, useProjects } from '~/stores/projects'
import { useSessionMarks } from '~/stores/session-marks'
import { useSessionSelection } from '~/stores/session-selection'
import { useSessionView } from '~/stores/session-view'
import { isPinned, toSessionRow, useSessions } from '~/stores/sessions'
import { useUi } from '~/stores/ui'
import {
  FOLDER_PREVIEW,
  fileSessions,
  folderPreview,
  SESSION_DRAG_TYPE,
} from '~/views/projects/logic'
import {
  agentIds,
  filterRows,
  hasActiveFilter,
  orderRows,
  sectionRows,
  visibleSlots,
} from './logic'
import { ProjectFolders } from './ProjectFolders'
import { useAdoptLocalPins } from './session-actions'
import { SessionBulkDelete } from './SessionBulk'
import { SessionRowLink } from './SessionRow'
import { SessionViewMenu } from './SessionViewMenu'

export { sessionPath } from './SessionRow'

/**
 * The pinned sessions, then the project folders, then the loose sessions in
 * the sections the view menu asks for (by date unless told otherwise). A
 * pin is the gateway's (`sessions.pin`), so the Pinned block matches the web
 * console's and what the agent pinned. A session filed in a project lives
 * under its folder, not in the list (a pinned one shows in both places);
 * while a search or a project filter is active every match shows flat so
 * nothing hides in a closed folder. Dragging a session onto the "Sessions"
 * header unfiles it.
 */
export function SessionList() {
  const query = useUi((s) => s.sessionQuery)
  const { rows, loading, error } = useSessions()
  const projectsState = useProjects()
  const { move } = useMoveSession()
  const view = useSessionView((s) => s.view)
  const setView = useSessionView((s) => s.set)
  const archived = useSessionMarks((s) => s.archived)
  const unread = useSessionMarks((s) => s.unread)
  const liveIds = useLive((s) => s.ids)
  const openFolders = useUi((s) => s.openFolders)
  const expandedFolders = useUi((s) => s.expandedFolders)
  const setVisible = useSessionSelection((s) => s.setVisible)
  const [over, setOver] = useState(false)
  const searching = Boolean(query.trim())
  const { projects } = projectsState
  useAdoptLocalPins(rows, !loading && !error)

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

  // Pinned rows sit above the folders, in the view's order, filed or not.
  const pinned = useMemo(
    () => orderRows(matched.filter(isPinned), view, { unread }),
    [matched, view, unread],
  )

  const sections = useMemo(() => {
    // Folders hold their own rows; a pinned row is in the block on top.
    const unfiled = showFolders ? new Set(filed.unfiled.map((r) => r.key)) : null
    const loose = matched.filter((r) => !isPinned(r) && (unfiled === null || unfiled.has(r.key)))
    const ordered = orderRows(loose, view, { unread })
    return sectionRows(
      ordered,
      view,
      {
        today: t('group.today'),
        yesterday: t('group.yesterday'),
        week: t('group.week'),
        noProject: t('view.section.noProject'),
        agent: (id) => id,
      },
      projects,
    )
  }, [showFolders, filed, matched, unread, view, projects])

  // What a Shift-click range runs over, and all a selection may hold: a row
  // hidden by a search, a filter or a closed folder drops out of it.
  const visible = useMemo(() => {
    const folders = showFolders
      ? projects
          .filter((p) => openFolders.has(projectId(p)))
          .map(
            (p) =>
              folderPreview(
                filed.byProject.get(projectId(p)) ?? [],
                FOLDER_PREVIEW,
                expandedFolders.has(projectId(p)),
              ).shown,
          )
      : []
    return visibleSlots(folders, sections, pinned)
  }, [showFolders, projects, openFolders, expandedFolders, filed, sections, pinned])
  useEffect(() => setVisible(visible), [visible, setVisible])
  useEffect(() => () => setVisible([]), [setVisible])

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
      {pinned.length > 0 ? (
        <section aria-label={t('view.section.pinned')}>
          <div className="mac-section">{t('view.section.pinned')}</div>
          {pinned.map((row) => (
            <SessionRowLink key={row.key} row={row} />
          ))}
        </section>
      ) : null}
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
      <SessionBulkDelete />
    </div>
  )
}
