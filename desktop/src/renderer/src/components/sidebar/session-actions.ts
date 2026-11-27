import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { exportMarkdownDocument, type ExportMessage } from '@/views/chat/logic'
import { t } from '~/i18n'
import { errorText } from '~/stores/projects'
import { useComposerDrafts } from '~/stores/composer-drafts'
import { useSessionMarks } from '~/stores/session-marks'
import type { SessionRow, SessionsList } from '~/stores/sessions'

interface HistoryMessage {
  role?: string
  text?: string
  timestamp?: number | string | null
}

/** Enough of a transcript for an export; the chat view pages at 50. */
const EXPORT_LIMIT = 500

/** `sessions.delete` answers per key: the ones it removed, and `"<key>: <why>"` for the rest. */
export interface DeleteReply {
  deleted?: unknown
  errors?: unknown
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((x): x is string => typeof x === 'string') : []
}

/**
 * Which of `keys` a `sessions.delete` reply says are gone. A reply without a
 * `deleted` list counts every key the errors do not name.
 */
export function deleteOutcome(
  keys: readonly string[],
  reply: DeleteReply | null | undefined,
): { deleted: string[]; failed: string[]; reasons: string[] } {
  const reasons = strings(reply?.errors)
  const listed = Array.isArray(reply?.deleted) ? new Set(strings(reply.deleted)) : null
  // Keys carry colons, so an error is matched on the whole "<key>: " prefix.
  const gone = (k: string) =>
    listed ? listed.has(k) : !reasons.some((r) => r.startsWith(`${k}: `))
  return {
    deleted: keys.filter(gone),
    failed: keys.filter((k) => !gone(k)),
    reasons: reasons.map((r) => {
      const key = keys.find((k) => r.startsWith(`${k}: `))
      return key ? r.slice(key.length + 2) : r
    }),
  }
}

/** The chat on screen is this session's. */
function showing(pathname: string, key: string): boolean {
  const path = decodeURIComponent(pathname)
  return path === `/sessions/${key}` || path.startsWith(`/sessions/${key}/`)
}

/**
 * What the row's menu can do to a session on the gateway: rename, export,
 * delete, copy its id (pinning is `usePinSessions`). The local marks
 * (archive, unread) are the store's own; the menu calls those directly.
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
    let reply: DeleteReply | undefined
    try {
      reply = await rpc.call<DeleteReply>('sessions.delete', { key })
    } catch (err) {
      toast.error(`${t('session.toast.deleteFailed')}: ${errorText(err)}`, {
        id: 'session-delete-err',
      })
      return false
    }
    // The gateway reports a key it could not delete in `errors`, not as a throw.
    const { failed, reasons } = deleteOutcome([key], reply)
    if (failed.length > 0) {
      const why = reasons[0]
      toast.error(
        why ? `${t('session.toast.deleteFailed')}: ${why}` : t('session.toast.deleteFailed'),
        {
          id: 'session-delete-err',
        },
      )
      return false
    }
    forget(key)
    useComposerDrafts.getState().forget(key)
    toast.success(t('session.toast.deleted'), { id: 'session-delete' })
    // The chat on screen was this session: leave it before the list refetches.
    if (showing(pathname, key)) {
      void navigate('/sessions', { replace: true })
    }
    await refresh()
    return true
  }, [rpc, key, forget, pathname, navigate, refresh])

  return { rename, copyId, exportMarkdown, remove }
}

/**
 * Pin or unpin sessions on the gateway (`sessions.pin`), so the Pinned
 * section is the same in the desktop app, the web console and for the agent.
 * The list is patched first and refetched after: the row moves at once, and
 * a call that fails puts it back with a toast.
 */
export function usePinSessions(): (keys: readonly string[], on: boolean) => Promise<void> {
  const rpc = useRpc()
  const queryClient = useQueryClient()

  return useCallback(
    async (keys, on) => {
      if (keys.length === 0) return
      // A refetch already in flight would land after the patch and undo it.
      await queryClient.cancelQueries({ queryKey: ['sessions'], exact: true })
      const which = new Set(keys)
      queryClient.setQueryData<SessionsList>(['sessions'], (data) =>
        data?.sessions
          ? {
              ...data,
              sessions: data.sessions.map((s) =>
                which.has(String(s.key || '')) ? { ...s, pinned: on } : s,
              ),
            }
          : data,
      )
      const results = await Promise.allSettled(
        keys.map((key) => rpc.call('sessions.pin', { key, pinned: on })),
      )
      const failed = results.find((r) => r.status === 'rejected')
      if (failed) {
        const title = on ? t('session.toast.pinFailed') : t('session.toast.unpinFailed')
        toast.error(`${title}: ${errorText(failed.reason)}`, { id: 'session-pin-err' })
      }
      await queryClient.invalidateQueries({ queryKey: ['sessions'] })
    },
    [rpc, queryClient],
  )
}

/**
 * Pins used to live in this app's localStorage only. Hand any that are left
 * to the gateway once, then forget them, so an upgrade does not empty the
 * Pinned section. A pin whose session is gone is simply dropped; one the
 * gateway refuses stays local for the next launch.
 */
export function useAdoptLocalPins(rows: readonly SessionRow[], ready: boolean): void {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const started = useRef(false)

  useEffect(() => {
    if (!ready || started.current) return
    const marks = useSessionMarks.getState()
    if (marks.pinned.size === 0) return
    started.current = true
    const known = new Map(rows.map((r) => [r.key, r]))
    void (async () => {
      let moved = false
      for (const key of marks.pinned) {
        const row = known.get(key)
        if (row && row.raw.pinned !== true) {
          try {
            await rpc.call('sessions.pin', { key, pinned: true })
            moved = true
          } catch {
            continue
          }
        }
        useSessionMarks.getState().setPinned(key, false)
      }
      if (moved) await queryClient.invalidateQueries({ queryKey: ['sessions'] })
    })()
  }, [rpc, queryClient, rows, ready])
}

/**
 * Delete several sessions in one `sessions.delete` call (the sidebar's
 * multi-selection). Resolves with the keys that went and the ones that did
 * not; a turn running in a deleted session is cancelled by the gateway.
 */
export function useRemoveSessions(): (
  keys: readonly string[],
) => Promise<{ deleted: string[]; failed: string[] }> {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const forget = useSessionMarks((s) => s.forget)

  return useCallback(
    async (keys) => {
      if (keys.length === 0) return { deleted: [], failed: [] }
      let reply: DeleteReply | undefined
      try {
        reply = await rpc.call<DeleteReply>('sessions.delete', { keys: [...keys] })
      } catch (err) {
        toast.error(`${t('session.bulk.toast.deleteFailed')}: ${errorText(err)}`, {
          id: 'session-delete-err',
        })
        return { deleted: [], failed: [...keys] }
      }
      const { deleted, failed } = deleteOutcome(keys, reply)
      for (const k of deleted) {
        forget(k)
        useComposerDrafts.getState().forget(k)
      }
      if (failed.length === 0) {
        toast.success(t('session.bulk.toast.deleted').replace('{n}', String(deleted.length)), {
          id: 'session-delete',
        })
      } else {
        toast.error(
          t('session.bulk.toast.partial')
            .replace('{done}', String(deleted.length))
            .replace('{total}', String(keys.length)),
          { id: 'session-delete-err' },
        )
      }
      if (deleted.some((k) => showing(pathname, k))) {
        void navigate('/sessions', { replace: true })
      }
      await queryClient.invalidateQueries({ queryKey: ['sessions'] })
      return { deleted, failed }
    },
    [rpc, queryClient, navigate, pathname, forget],
  )
}

function downloadText(text: string, filename: string): void {
  const blob = new Blob([text], { type: 'text/markdown' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}
