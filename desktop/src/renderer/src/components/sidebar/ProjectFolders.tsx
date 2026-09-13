import '~/views/projects/projects.css'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { ChevronRight, Folder, Plus } from 'lucide-react'
import { useEffect, useRef, useState, type DragEvent } from 'react'
import { NavLink, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { projectId, projectName, type RawProject } from '@/views/projects/logic'
import { t } from '~/i18n'
import { quick } from '~/lib/motion'
import { errorText, invalidateProjects, useAgents, useMoveSession } from '~/stores/projects'
import type { SessionRow } from '~/stores/sessions'
import { useUi } from '~/stores/ui'
import { folderPreview, normalizeName, SESSION_DRAG_TYPE, type Filed } from '~/views/projects/logic'
import { SessionRowLink } from './SessionRow'

/** Route path for a project's page. */
export function projectPath(id: string): string {
  return `/projects/${encodeURIComponent(id)}`
}

function hasSessionDrag(e: DragEvent): boolean {
  return Array.from(e.dataTransfer?.types ?? []).includes(SESSION_DRAG_TYPE)
}

/**
 * Project folders in the source list, the way Notes keeps folders above its
 * notes: a disclosure per project with that project's chats inside, a "+"
 * that opens an inline name row (no dialog), and every folder a drop target
 * for a session dragged from anywhere in the sidebar.
 */
export function ProjectFolders({
  projects,
  filed,
  loading,
}: {
  projects: RawProject[]
  filed: Filed
  loading: boolean
}) {
  const creating = useUi((s) => s.creatingProject)
  const startCreating = useUi((s) => s.startCreatingProject)
  const showHint = !loading && !creating && projects.length === 0

  return (
    <div className="proj-folders">
      <div className="mac-section flex items-center justify-between">
        <span>{t('projects.section')}</span>
        <button
          type="button"
          className="proj-folders__add app-no-drag"
          aria-label={t('projects.new')}
          title={t('projects.new')}
          onClick={startCreating}
        >
          <Plus className="size-3.5" strokeWidth={2} aria-hidden />
        </button>
      </div>
      {creating ? <NewProjectRow /> : null}
      {loading ? (
        <p className="px-2.5 py-1 text-[11.5px] text-dim">{t('projects.loading')}</p>
      ) : null}
      {showHint ? <p className="px-2.5 py-1 text-[11.5px] text-dim">{t('projects.hint')}</p> : null}
      {projects.map((p) => (
        <FolderRow key={projectId(p)} project={p} rows={filed.byProject.get(projectId(p)) ?? []} />
      ))}
    </div>
  )
}

function FolderRow({ project, rows }: { project: RawProject; rows: SessionRow[] }) {
  const id = projectId(project)
  const name = projectName(project)
  const open = useUi((s) => s.openFolders.has(id))
  const toggle = useUi((s) => s.toggleFolder)
  const setOpen = useUi((s) => s.setFolderOpen)
  const reduce = useReducedMotion()
  const { move } = useMoveSession()
  const [over, setOver] = useState(false)
  const { shown, hidden } = folderPreview(rows)

  function onDragOver(e: DragEvent) {
    if (!hasSessionDrag(e)) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (!over) setOver(true)
  }
  function onDrop(e: DragEvent) {
    if (!hasSessionDrag(e)) return
    e.preventDefault()
    setOver(false)
    const key = e.dataTransfer.getData(SESSION_DRAG_TYPE)
    if (!key) return
    if (rows.some((r) => r.key === key)) return
    move(key, project)
    setOpen(id, true)
  }

  return (
    <div className="proj-folder" data-open={open} data-drop={over}>
      <div
        className="proj-folder__row"
        onDragOver={onDragOver}
        onDragEnter={onDragOver}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <button
          type="button"
          className="proj-folder__disclose app-no-drag"
          aria-label={open ? t('projects.folder.close') : t('projects.folder.open')}
          aria-expanded={open}
          onClick={() => toggle(id)}
        >
          <ChevronRight className="size-3" strokeWidth={2} aria-hidden />
        </button>
        <NavLink to={projectPath(id)} className="proj-folder__link" title={name}>
          <Folder className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden />
          <span className="proj-folder__name">{name}</span>
          <span className="proj-folder__count">{rows.length || ''}</span>
        </NavLink>
        {over ? <span className="proj-folder__drop">{t('projects.folder.drop')}</span> : null}
      </div>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.div
            key="body"
            className="proj-folder__body"
            initial={reduce ? false : { height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={reduce ? undefined : { height: 0, opacity: 0 }}
            transition={quick}
          >
            {shown.length === 0 ? (
              <p className="proj-folder__empty">{t('projects.folder.empty')}</p>
            ) : null}
            {shown.map((row) => (
              <SessionRowLink key={row.key} row={row} nested />
            ))}
            {hidden > 0 ? (
              <NavLink to={projectPath(id)} className="proj-folder__more">
                {hidden} {t('projects.folder.more')}
              </NavLink>
            ) : null}
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  )
}

/**
 * Finder's "New Folder": an editable row appears in place, Return creates,
 * Escape (or blurring it empty) discards. The agent picker only shows when
 * the registry has more than one agent; a project's default agent is fixed
 * at creation, so this is the one moment to choose it.
 */
function NewProjectRow() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const stop = useUi((s) => s.stopCreatingProject)
  const setOpen = useUi((s) => s.setFolderOpen)
  const agents = useAgents()
  const [name, setName] = useState('')
  const [agentId, setAgentId] = useState('main')
  const inputRef = useRef<HTMLInputElement>(null)
  const submittedRef = useRef(false)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const create = useMutation({
    mutationFn: (vars: { name: string; agentId: string }) =>
      rpc.call<{ project?: RawProject }>('projects.create', {
        name: vars.name,
        agentId: vars.agentId,
        knowledge: '',
      }),
    onSuccess: (data) => {
      toast.success(t('projects.toast.created'), { id: 'projects-create' })
      invalidateProjects(queryClient)
      stop()
      const id = data?.project ? projectId(data.project) : ''
      if (id) {
        setOpen(id, true)
        void navigate(projectPath(id))
      }
    },
    onError: (err) => {
      submittedRef.current = false
      toast.error(`${t('projects.toast.createFailed')}: ${errorText(err)}`, {
        id: 'projects-create-err',
      })
    },
  })

  function submit() {
    if (submittedRef.current) return
    const clean = normalizeName(name)
    if (!clean) {
      stop()
      return
    }
    submittedRef.current = true
    create.mutate({ name: clean, agentId })
  }

  return (
    <div className="proj-new app-no-drag" data-busy={create.isPending}>
      <div className="proj-new__row">
        <Folder className="size-3.5 shrink-0 text-primary" strokeWidth={1.75} aria-hidden />
        <input
          ref={inputRef}
          className="proj-new__input"
          value={name}
          placeholder={t('projects.new.placeholder')}
          aria-label={t('projects.new')}
          maxLength={200}
          spellCheck={false}
          disabled={create.isPending}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submit()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              stop()
            }
          }}
          onBlur={(e) => {
            // Moving to the agent picker is not leaving the row.
            if (e.relatedTarget instanceof HTMLElement && e.relatedTarget.closest('.proj-new')) {
              return
            }
            submit()
          }}
        />
      </div>
      {agents.length > 1 ? (
        <label className="proj-new__agent">
          <span>{t('projects.new.agent')}</span>
          <select
            className="mac-select"
            data-compact="true"
            value={agentId}
            disabled={create.isPending}
            onChange={(e) => setAgentId(e.target.value)}
            onBlur={(e) => {
              if (e.relatedTarget instanceof HTMLElement && e.relatedTarget.closest('.proj-new')) {
                return
              }
              submit()
            }}
          >
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name || a.id}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <p className="proj-new__hint">{t('projects.new.hint')}</p>
    </div>
  )
}
