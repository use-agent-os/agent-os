import { useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { exportMarkdownDocument, type ExportMessage } from '@/views/chat/logic'
import { t } from '~/i18n'
import { errorText } from '~/stores/projects'
import { useSessionMarks } from '~/stores/session-marks'
import type { SessionRow } from '~/stores/sessions'

interface HistoryMessage {
  role?: string
  text?: string
  timestamp?: number | string | null
}

/** Enough of a transcript for an export; the chat view pages at 50. */
const EXPORT_LIMIT = 500

/**
 * What the row's menu can do to a session on the gateway: rename, export,
 * delete, copy its id. The local marks (pin, archive, unread) are the
 * store's own; the menu calls those directly.
 */
export function useSessionActions(row: SessionRow): {
  rename: (name: string) => Promise<void>
  copyId: () => Promise<void>
  exportMarkdown: () => Promise<void>
  remove: () => Promise<boolean>
} {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const forget = useSessionMarks((s) => s.forget)
  const key = row.key

  const refresh = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ['sessions'] }),
    [queryClient],
  )

  const rename = useCallback(
    async (name: string) => {
      const clean = name.trim()
      if (!clean || clean === row.title) return
      try {
        await rpc.call('sessions.rename', { key, name: clean })
        toast.success(t('session.toast.renamed'), { id: 'session-rename' })
      } catch (err) {
        toast.error(`${t('session.toast.renameFailed')}: ${errorText(err)}`, {
          id: 'session-rename-err',
        })
      }
      await refresh()
    },
    [rpc, key, row.title, refresh],
  )

  const copyId = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(key)
      toast.success(t('session.toast.copied'), { id: 'session-copy' })
    } catch {
      toast.error(t('session.toast.copyFailed'), { id: 'session-copy' })
    }
  }, [key])

  const exportMarkdown = useCallback(async () => {
    try {
      const data = await rpc.call<{ messages?: HistoryMessage[] }>('chat.history', {
        sessionKey: key,
        limit: EXPORT_LIMIT,
        includeCanonical: false,
        includeSummaries: false,
      })
      const messages: ExportMessage[] = (data?.messages ?? [])
        .filter((m) => m.role && typeof m.text === 'string')
        .map((m) => ({ role: String(m.role), text: m.text ?? '', ts: m.timestamp ?? undefined }))
      const md = exportMarkdownDocument(messages, key)
      if (md === null) {
        toast.warning(t('session.toast.exportEmpty'), { id: 'session-export' })
        return
      }
      downloadText(md, `chat-${key}.md`)
      toast.info(t('session.toast.exported'), { id: 'session-export' })
    } catch (err) {
      toast.error(`${t('session.toast.exportFailed')}: ${errorText(err)}`, {
        id: 'session-export',
      })
    }
  }, [rpc, key])

  const remove = useCallback(async () => {
    try {
      await rpc.call('sessions.delete', { key })
    } catch (err) {
      toast.error(`${t('session.toast.deleteFailed')}: ${errorText(err)}`, {
        id: 'session-delete-err',
      })
      return false
    }
    forget(key)
    toast.success(t('session.toast.deleted'), { id: 'session-delete' })
    // The chat on screen was this session: leave it before the list refetches.
    if (decodeURIComponent(pathname).startsWith(`/sessions/${key}`)) {
      void navigate('/sessions', { replace: true })
    }
    await refresh()
    return true
  }, [rpc, key, forget, pathname, navigate, refresh])

  return { rename, copyId, exportMarkdown, remove }
}

function downloadText(text: string, filename: string): void {
  const blob = new Blob([text], { type: 'text/markdown' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}
