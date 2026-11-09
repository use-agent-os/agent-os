import { toast } from 'sonner'
import { t } from '@/i18n'
import '@/i18n/en/webShell'
import type { WsRpcClient } from '@/lib/ws-rpc'
import type { HistoryResponse } from '@/views/chat/transcript/history'

/**
 * Download a session's transcript as Markdown. The export builder rides in
 * its own chunk: it is rarely used and pulls in the chat logic module.
 */
export async function exportSessionMarkdown(
  rpc: Pick<WsRpcClient, 'call'>,
  key: string,
): Promise<void> {
  const history = await rpc.call<HistoryResponse>('chat.history', {
    sessionKey: key,
    limit: 500,
    includeCanonical: false,
    includeSummaries: false,
  })
  const { exportMarkdownDocument } = await import('@/views/chat/logic')
  const md = exportMarkdownDocument(
    (history?.messages ?? []).map((m) => ({ role: m.role, text: m.text, ts: m.timestamp })),
    key,
  )
  if (md === null) {
    toast.warning(t('webShell.exportEmpty'))
    return
  }
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }))
  a.download = `chat-${key}.md`
  a.click()
  URL.revokeObjectURL(a.href)
  toast.info(t('webShell.exported'))
}
