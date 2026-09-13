import { ChevronDown, Folder, FolderPlus } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { projectId, projectName, sessionProjectId } from '@/views/projects/logic'
import { Menu, MenuItem, MenuNote, MenuSep } from '~/components/menu/PopMenu'
import { t } from '~/i18n'
import { useMoveSession, useProjects } from '~/stores/projects'
import { useSessions } from '~/stores/sessions'
import { useUi } from '~/stores/ui'

/**
 * Where this chat is filed, as a chip in the conversation header. Click to
 * move it to another folder or take it out of one. Only for a session the
 * gateway already knows: a fresh, unsent chat has nothing to file yet.
 */
export function ProjectChip({ sessionKey }: { sessionKey: string }) {
  const { projects, byId } = useProjects()
  const { rows } = useSessions()
  const { move, pending } = useMoveSession()
  const startCreating = useUi((s) => s.startCreatingProject)
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])

  const currentId = useMemo(() => {
    const row = rows.find((r) => r.key === sessionKey)
    return row ? sessionProjectId(row.raw) : ''
  }, [rows, sessionKey])
  const current = currentId ? byId.get(currentId) : undefined
  const known = rows.some((r) => r.key === sessionKey)
  if (!known) return null

  return (
    <div className="proj-chip__anchor">
      <button
        type="button"
        className="proj-chip app-no-drag"
        data-filed={Boolean(current)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('projects.menu.label')}
        title={current ? projectName(current) : t('projects.menu.add')}
        disabled={pending}
        onClick={() => setOpen((v) => !v)}
      >
        {current ? (
          <Folder className="size-3.5" strokeWidth={1.75} aria-hidden />
        ) : (
          <FolderPlus className="size-3.5" strokeWidth={1.75} aria-hidden />
        )}
        <span className="proj-chip__name">
          {current ? projectName(current) : t('projects.menu.add')}
        </span>
        <ChevronDown className="size-3 opacity-60" strokeWidth={2} aria-hidden />
      </button>
      {open ? (
        <Menu onClose={close} label={t('projects.menu.label')} align="start">
          {projects.length === 0 ? <MenuNote>{t('projects.menu.empty')}</MenuNote> : null}
          {projects.map((p) => {
            const id = projectId(p)
            const selected = id === currentId
            return (
              <MenuItem
                key={id}
                role="menuitemradio"
                checked={selected}
                label={projectName(p)}
                onSelect={() => {
                  if (!selected) move(sessionKey, p)
                }}
              />
            )
          })}
          {current ? (
            <>
              <MenuSep />
              <MenuItem label={t('projects.menu.remove')} onSelect={() => move(sessionKey, null)} />
            </>
          ) : null}
          <MenuSep />
          <MenuItem icon={FolderPlus} label={t('projects.menu.create')} onSelect={startCreating} />
        </Menu>
      ) : null}
    </div>
  )
}
