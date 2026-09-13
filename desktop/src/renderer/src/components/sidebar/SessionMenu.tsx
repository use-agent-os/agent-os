import {
  Archive,
  ArchiveRestore,
  Copy,
  Download,
  FolderInput,
  Mail,
  MailOpen,
  Pencil,
  Pin,
  PinOff,
  Trash2,
} from 'lucide-react'
import { projectId, projectName, sessionProjectId } from '@/views/projects/logic'
import { MenuItem, MenuNote, MenuSep, MenuSub } from '~/components/menu/PopMenu'
import { t } from '~/i18n'
import { toast } from 'sonner'
import { useMoveSession, useProjects } from '~/stores/projects'
import { useSessionMarks } from '~/stores/session-marks'
import type { SessionRow } from '~/stores/sessions'
import type { useSessionActions } from './session-actions'

/**
 * The rows of a session's context menu. Rendered inside a PopMenu; the
 * parent owns the two things that outlive the menu (the rename field and
 * the delete alert).
 */
export function SessionMenuItems({
  row,
  actions,
  onRename,
  onDelete,
}: {
  row: SessionRow
  actions: ReturnType<typeof useSessionActions>
  onRename: () => void
  onDelete: () => void
}) {
  const marks = useSessionMarks()
  const { projects, byId } = useProjects()
  const { move } = useMoveSession()
  const pinned = marks.pinned.has(row.key)
  const archived = marks.archived.has(row.key)
  const unread = marks.unread.has(row.key)
  const currentProject = sessionProjectId(row.raw)
  const currentName = currentProject ? byId.get(currentProject) : undefined

  return (
    <>
      <MenuItem icon={Pencil} label={t('session.menu.rename')} onSelect={onRename} />
      <MenuItem
        icon={pinned ? PinOff : Pin}
        label={pinned ? t('session.menu.unpin') : t('session.menu.pin')}
        onSelect={() => marks.setPinned(row.key, !pinned)}
      />
      <MenuItem
        icon={unread ? MailOpen : Mail}
        label={unread ? t('session.menu.markRead') : t('session.menu.markUnread')}
        onSelect={() => marks.setUnread(row.key, !unread)}
      />
      <MenuItem
        icon={Copy}
        label={t('session.menu.copyId')}
        onSelect={() => void actions.copyId()}
      />
      <MenuSep />
      <MenuItem
        icon={Download}
        label={t('session.menu.export')}
        onSelect={() => void actions.exportMarkdown()}
      />
      <MenuSub
        icon={FolderInput}
        label={t('session.menu.moveTo')}
        value={currentName ? projectName(currentName) : undefined}
      >
        {projects.length === 0 ? <MenuNote>{t('projects.menu.empty')}</MenuNote> : null}
        {projects.map((p) => {
          const id = projectId(p)
          const selected = id === currentProject
          return (
            <MenuItem
              key={id}
              role="menuitemradio"
              checked={selected}
              label={projectName(p)}
              onSelect={() => {
                if (!selected) move(row.key, p)
              }}
            />
          )
        })}
        {projects.length > 0 ? <MenuSep /> : null}
        <MenuItem
          role="menuitemradio"
          checked={!currentProject}
          label={t('session.menu.noProject')}
          onSelect={() => {
            if (currentProject) move(row.key, null)
          }}
        />
      </MenuSub>
      <MenuSep />
      <MenuItem
        icon={archived ? ArchiveRestore : Archive}
        label={archived ? t('session.menu.unarchive') : t('session.menu.archive')}
        onSelect={() => {
          marks.setArchived(row.key, !archived)
          toast.success(archived ? t('session.toast.unarchived') : t('session.toast.archived'), {
            id: 'session-archive',
          })
        }}
      />
      <MenuItem icon={Trash2} tone="danger" label={t('session.menu.delete')} onSelect={onDelete} />
    </>
  )
}
