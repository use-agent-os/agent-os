import './projects.css'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Folder, FolderX, MessageSquarePlus, MoreHorizontal, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { ModalShell } from '@/components/ModalShell'
import { projectAgentId, projectId, projectName, type RawProject } from '@/views/projects/logic'
import { Menu, MenuItem } from '~/components/menu/PopMenu'
import { sessionPath } from '~/components/sidebar/SessionRow'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { shortAge } from '~/lib/relative-time'
import { useNow } from '~/lib/use-now'
import { useGateway } from '~/stores/gateway'
import { useLive } from '~/stores/live'
import { errorText, invalidateProjects, useProjects } from '~/stores/projects'
import { useSessions, type SessionRow } from '~/stores/sessions'
import { useUi } from '~/stores/ui'
import { BriefEditor } from './BriefEditor'
import { briefDate, groupByAgent, initials, normalizeName } from './logic'

function toEpochMs(value: unknown): number {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return 0
  return n < 1e12 ? n * 1000 : n
}

/**
 * A project's page: the folder's name as a document title you click to
 * rename, the brief as a page of text that saves itself, and the chats filed
 * here. Reached from a folder in the sidebar; nothing here is a form.
 */
export function ProjectView() {
  const gatewayState = useGateway((s) => s.status.state)
  const { id: rawId } = useParams()
  const id = rawId ? decodeURIComponent(rawId) : ''
  if (gatewayState !== 'running') {
    return (
      <div className="proj-page__offline">
        <p className="text-[15px] font-semibold text-foreground">
          {gatewayState === 'starting'
            ? t('chat.waitingGateway')
            : t(`gateway.state.${gatewayState}`)}
        </p>
        <p className="max-w-sm">{t('chat.gatewayDown')}</p>
      </div>
    )
  }
  return <ConnectedProject key={id} id={id} />
}

function ConnectedProject({ id }: { id: string }) {
  const { byId, loading } = useProjects()
  const project = byId.get(id)

  if (!project) {
    if (loading) return null
    return (
      <div className="proj-page__offline">
        <FolderX className="size-8 text-dim" strokeWidth={1.25} aria-hidden />
        <p className="text-[15px] font-semibold text-foreground">{t('projects.page.missing')}</p>
        <p className="max-w-sm">{t('projects.page.missing.body')}</p>
        <Link to="/sessions" className="mac-button mt-2">
          {t('projects.page.back')}
        </Link>
      </div>
    )
  }
  return <ProjectPage project={project} />
}

function ProjectPage({ project }: { project: RawProject }) {
  const rpc = useRpc()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const setFolderOpen = useUi((s) => s.setFolderOpen)
  const { rows } = useSessions()
  const now = useNow(30_000)
  const id = projectId(project)
  const name = projectName(project)
  const agentId = projectAgentId(project) || 'main'
  const updatedAt = toEpochMs(project.updated_at ?? project.updatedAt)
  const createdAt = toEpochMs(project.created_at ?? project.createdAt)
  const knowledge = String(project.knowledge ?? '')

  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  // The folder in the sidebar follows the page.
  useEffect(() => setFolderOpen(id, true), [id, setFolderOpen])

  const sessions = useMemo(
    () => rows.filter((r) => String(r.raw.project_id || r.raw.projectId || '') === id),
    [rows, id],
  )
  const groups = useMemo(() => groupByAgent(sessions), [sessions])

  // ── Save (name or brief), compare-and-swap on updated_at ─────────────────
  const update = useMutation({
    mutationFn: (vars: { name?: string; knowledge?: string; expected: number }) =>
      rpc.call<{ project?: RawProject }>('projects.update', {
        projectId: id,
        ...(vars.name !== undefined ? { name: vars.name } : {}),
        ...(vars.knowledge !== undefined ? { knowledge: vars.knowledge } : {}),
        expectedUpdatedAt: vars.expected,
      }),
    onSuccess: (data, vars) => {
      const fresh = data?.project
      if (fresh) {
        // Patch the cache so the page never shows the pre-save value between
        // the response and the refetch.
        queryClient.setQueryData<{ projects?: RawProject[] }>(['projects'], (prev) =>
          prev
            ? {
                ...prev,
                projects: (prev.projects ?? []).map((p) =>
                  projectId(p) === id ? { ...p, ...fresh } : p,
                ),
              }
            : prev,
        )
      }
      if (vars.name !== undefined) {
        toast.success(t('projects.toast.renamed'), { id: 'projects-update' })
      }
      invalidateProjects(queryClient)
    },
    onError: (err) => {
      const code = (err as { code?: string }).code
      if (code === 'project.conflict') {
        toast.error(t('projects.toast.conflict'), { id: 'projects-update-err' })
        invalidateProjects(queryClient)
        return
      }
      toast.error(`${t('projects.toast.saveFailed')}: ${errorText(err)}`, {
        id: 'projects-update-err',
      })
    },
  })

  const rawUpdated = Number(project.updated_at ?? project.updatedAt)
  const saveBrief = useCallback(
    (text: string) => update.mutateAsync({ knowledge: text, expected: rawUpdated }),
    [update, rawUpdated],
  )

  function rename(next: string) {
    const clean = normalizeName(next)
    if (!clean || clean === name) return
    update.mutate({ name: clean, expected: rawUpdated })
  }

  // ── New chat in this folder ──────────────────────────────────────────────
  const newChat = useMutation({
    mutationFn: () => rpc.call<{ key?: string }>('sessions.create', { agentId, projectId: id }),
    onSuccess: (res) => {
      invalidateProjects(queryClient)
      if (res?.key) void navigate(sessionPath(res.key))
    },
    onError: (err) =>
      toast.error(`${t('projects.toast.chatFailed')}: ${errorText(err)}`, {
        id: 'projects-chat-err',
      }),
  })

  // ── Delete ───────────────────────────────────────────────────────────────
  const remove = useMutation({
    mutationFn: () => rpc.call('projects.delete', { projectId: id }),
    onSuccess: () => {
      toast.success(t('projects.toast.deleted'), { id: 'projects-delete' })
      setConfirmDelete(false)
      invalidateProjects(queryClient)
      void navigate('/sessions', { replace: true })
    },
    onError: (err) =>
      toast.error(`${t('projects.toast.deleteFailed')}: ${errorText(err)}`, {
        id: 'projects-delete-err',
      }),
  })

  const count = sessions.length
  const countWord = count === 1 ? t('projects.page.chats.one') : t('projects.page.chats.many')

  return (
    <div className="proj-page" data-selectable>
      <header className="proj-head">
        <div className="proj-head__glyph" aria-hidden>
          {initials(name)}
        </div>
        <div className="proj-head__text">
          <div className="proj-head__eyebrow">
            <Folder className="size-3" strokeWidth={2} aria-hidden />
            <span>{t('projects.page.eyebrow')}</span>
            <span className="proj-head__dot" aria-hidden />
            <span className="proj-head__agent" title={t('projects.chats.agent')}>
              {agentId}
            </span>
          </div>
          <TitleField name={name} disabled={update.isPending} onRename={rename} />
          <p className="proj-head__meta">
            <span>
              {count} {countWord}
            </span>
            {updatedAt ? (
              <>
                <span className="proj-head__dot" aria-hidden />
                <span title={new Date(updatedAt).toLocaleString()}>
                  {t('projects.page.updated')} {briefDate(updatedAt, now)}
                </span>
              </>
            ) : null}
            {createdAt ? (
              <>
                <span className="proj-head__dot" aria-hidden />
                <span title={new Date(createdAt).toLocaleString()}>
                  {t('projects.page.created')} {briefDate(createdAt, now)}
                </span>
              </>
            ) : null}
          </p>
        </div>
        <div className="proj-head__actions">
          <Button variant="primary" disabled={newChat.isPending} onClick={() => newChat.mutate()}>
            <MessageSquarePlus className="size-3.5" strokeWidth={2} aria-hidden />
            {newChat.isPending ? t('projects.page.newChat.busy') : t('projects.page.newChat')}
          </Button>
          <div className="proj-more">
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('projects.page.more')}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <MoreHorizontal
                className="size-4 text-muted-foreground"
                strokeWidth={1.75}
                aria-hidden
              />
            </Button>
            {menuOpen ? (
              <Menu onClose={() => setMenuOpen(false)}>
                <MenuItem
                  icon={Trash2}
                  tone="danger"
                  label={t('projects.page.delete')}
                  onSelect={() => setConfirmDelete(true)}
                />
              </Menu>
            ) : null}
          </div>
        </div>
      </header>

      <BriefEditor key={id} projectId={id} saved={knowledge} onSave={saveBrief} />

      <section className="proj-chats" aria-labelledby="proj-chats-title">
        <div className="proj-chats__head">
          <h2 id="proj-chats-title">{t('projects.chats.title')}</h2>
          <span className="proj-chats__count">{count || ''}</span>
        </div>
        {count === 0 ? (
          <div className="proj-chats__empty">
            <p>{t('projects.chats.empty')}</p>
            <p className="proj-chats__empty-hint">{t('projects.chats.empty.hint')}</p>
            <Button disabled={newChat.isPending} onClick={() => newChat.mutate()}>
              <MessageSquarePlus className="size-3.5" strokeWidth={2} aria-hidden />
              {t('projects.chats.start')}
            </Button>
          </div>
        ) : (
          groups.map((g) => (
            <div key={g.agentId} className="proj-chats__group">
              {groups.length > 1 ? (
                <div className="proj-chats__agent">
                  {t('projects.chats.agent')} · {g.agentId}
                </div>
              ) : null}
              <ul className="proj-chats__list">
                {g.items.map((row) => (
                  <ChatRow key={row.key} row={row} now={now} />
                ))}
              </ul>
            </div>
          ))
        )}
      </section>

      {confirmDelete ? (
        <DeleteConfirm
          name={name}
          busy={remove.isPending}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => remove.mutate()}
        />
      ) : null}
    </div>
  )
}

/** The name as the page title. Click to edit, Return or blur commits, Escape reverts. */
function TitleField({
  name,
  disabled,
  onRename,
}: {
  name: string
  disabled: boolean
  onRename: (next: string) => void
}) {
  const [draft, setDraft] = useState(name)
  const [prev, setPrev] = useState(name)
  if (prev !== name) {
    setPrev(name)
    setDraft(name)
  }
  const inputRef = useRef<HTMLInputElement>(null)
  return (
    <input
      ref={inputRef}
      className="proj-title app-no-drag"
      value={draft}
      aria-label={t('projects.page.rename')}
      title={t('projects.page.rename.hint')}
      maxLength={200}
      spellCheck={false}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          inputRef.current?.blur()
        } else if (e.key === 'Escape') {
          e.preventDefault()
          setDraft(name)
          inputRef.current?.blur()
        }
      }}
      onBlur={() => {
        if (!normalizeName(draft)) {
          setDraft(name)
          return
        }
        onRename(draft)
      }}
    />
  )
}

function ChatRow({ row, now }: { row: SessionRow; now: number }) {
  const live = useLive((s) => s.ids.has(row.key)) || row.live
  return (
    <li>
      <Link to={sessionPath(row.key)} className="proj-chat app-no-drag" title={row.key}>
        <span className="mac-session-dot" data-live={live} aria-hidden />
        <span className="proj-chat__title">{row.title}</span>
        <span className="proj-chat__age">{row.updatedAt ? shortAge(row.updatedAt, now) : ''}</span>
      </Link>
    </li>
  )
}

function DeleteConfirm({
  name,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string
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
      overlayClassName="proj-alert__overlay"
      className="proj-alert"
    >
      <h2 id={titleId} className="proj-alert__title">
        {t('projects.delete.title')}
      </h2>
      <p id={bodyId} className="proj-alert__body">
        <strong>{name}</strong> — {t('projects.delete.body')}
      </p>
      <div className="proj-alert__actions">
        <Button disabled={busy} onClick={onCancel}>
          {t('projects.delete.cancel')}
        </Button>
        <Button variant="danger" disabled={busy} onClick={onConfirm}>
          {t('projects.delete.confirm')}
        </Button>
      </div>
    </ModalShell>
  )
}
