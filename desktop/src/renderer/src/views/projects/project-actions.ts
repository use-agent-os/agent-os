import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { projectAgentId, projectId, projectName, type RawProject } from '@/views/projects/logic'
import { sessionPath } from '~/components/sidebar/SessionRow'
import { t } from '~/i18n'
import { errorText, invalidateProjects } from '~/stores/projects'
import { useUi } from '~/stores/ui'
import { normalizeName } from './logic'

/**
 * What can be done to a project on the gateway: rename it, save its brief,
 * start a chat filed in it, delete it. The project page and the folder's
 * context menu in the sidebar both go through here, so either place
 * conflict-checks a rename and leaves a deleted project's page alike.
 */
export function useProjectActions(project: RawProject): {
  /** A blank or unchanged name is a no-op. */
  rename: (name: string) => void
  /** Resolves when the gateway has the brief. */
  saveBrief: (text: string) => Promise<unknown>
  /** A chat filed here, with the project's agent, opened once created. */
  newChat: () => void
  /** True once the project is gone. */
  remove: () => Promise<boolean>
  saving: boolean
  starting: boolean
  deleting: boolean
} {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const setFolderOpen = useUi((s) => s.setFolderOpen)
  const id = projectId(project)
  const name = projectName(project)
  const agentId = projectAgentId(project) || 'main'
  const rawUpdated = Number(project.updated_at ?? project.updatedAt)

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

  // ── New chat in this folder ──────────────────────────────────────────────
  const newChat = useMutation({
    mutationFn: () => rpc.call<{ key?: string }>('sessions.create', { agentId, projectId: id }),
    onSuccess: (res) => {
      invalidateProjects(queryClient)
      // The new chat shows under its folder in the sidebar.
      setFolderOpen(id, true)
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
      invalidateProjects(queryClient)
      // Its page was on screen: leave it rather than land on "no longer exists".
      if (decodeURIComponent(pathname) === `/projects/${id}`) {
        void navigate('/sessions', { replace: true })
      }
    },
    onError: (err) =>
      toast.error(`${t('projects.toast.deleteFailed')}: ${errorText(err)}`, {
        id: 'projects-delete-err',
      }),
  })

  return {
    rename: (next) => {
      const clean = normalizeName(next)
      if (!clean || clean === name) return
      update.mutate({ name: clean, expected: rawUpdated })
    },
    saveBrief: (text) => update.mutateAsync({ knowledge: text, expected: rawUpdated }),
    newChat: () => newChat.mutate(),
    remove: () =>
      remove.mutateAsync().then(
        () => true,
        () => false,
      ),
    saving: update.isPending,
    starting: newChat.isPending,
    deleting: remove.isPending,
  }
}
