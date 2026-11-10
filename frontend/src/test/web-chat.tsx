// Harness for the chat workspace's tests: the real routes, a FakeRpc that
// answers the chat calls, and helpers to push session events.
import { act, screen } from '@testing-library/react'
import { usePreferences, DEFAULT_PREFERENCES } from '@/stores/preferences'
import { resetStagedAttachmentsForTests } from '@/web/features/chat/composer/drafts'
import { resetQueuesForTests } from '@/web/features/chat/composer/queue'
import { resetPanelsForTests } from '@/web/features/chat/panels/panel-store'
import type { FakeRpc } from './fake-rpc'
import { renderShell, resetShellState, sessionRow, shellRpc } from './web-shell'

export const KEY = 'agent:main:webchat:t1'
export const CHAT_PATH = '/chat/main/webchat/t1'

export function resetChatState(): void {
  resetShellState()
  resetPanelsForTests()
  resetStagedAttachmentsForTests()
  resetQueuesForTests()
  usePreferences.setState({ ...DEFAULT_PREFERENCES })
}

export function chatRpc(
  history: Record<string, unknown>[] = [],
  extra: Record<string, unknown> = {},
): FakeRpc {
  return shellRpc([sessionRow(KEY, { display_name: 'Test chat', ...extra })])
    .handle('chat.history', { messages: history, has_more: false, newest_cursor: '1|x' })
    .handle('sessions.messages.subscribe', {
      subscribed: true,
      current_stream_seq: 0,
      replay_complete: true,
    })
    .handle('sessions.messages.unsubscribe', null)
    .handle('chat.send', { ok: true, status: 'accepted' })
    .handle('chat.abort', { aborted: true })
    .handle('commands.list_for_surface', {
      commands: [
        {
          name: '/compact',
          description: 'Compact the context',
          execution: { action: 'compact_context' },
        },
        { name: '/new', description: 'New chat', execution: { action: 'new_chat' } },
      ],
    })
    .handle('router.hold.get', {
      enabled: true,
      provider: 'openrouter',
      hold: null,
      tiers: [{ tier: 'c1', model: 'small' }],
    })
    .handle('models.list', [{ id: 'anthropic/claude-opus', name: 'Claude Opus' }])
    .handle('router.hold.set', { ok: true })
    .handle('router.hold.clear', { ok: true })
    .handle('plan.mode.get', { planMode: false })
    .handle('plan.mode.set', { ok: true })
    .handle('config.get', {
      permissions: { default_mode: 'on' },
      agentos_router: { enabled: true },
    })
    .handle('usage.status', {
      sessions: [{ session: KEY, input_tokens: 100, output_tokens: 20, cost_usd: 0.01 }],
    })
    .handle('sessions.contextCompact', { ok: true })
    .handle('sessions.rename', { ok: true })
}

export async function openChat(rpc: FakeRpc = chatRpc(), path = CHAT_PATH) {
  const utils = renderShell(path, rpc)
  // The first test of a file also pays for loading the lazy chat chunk, which
  // can take longer than the default one second on a busy runner.
  const composer = await screen.findByRole('textbox', { name: 'Message' }, { timeout: 5000 })
  return { ...utils, composer }
}

export function emit(rpc: FakeRpc, kind: string, payload: Record<string, unknown> = {}) {
  act(() => rpc.emit(`session.event.${kind}`, { session_key: KEY, ...payload }))
}

export const userRow = (id: string, text: string, ts = 1_700_000_000) => ({
  id,
  message_id: id,
  role: 'user',
  text,
  timestamp: ts,
})
export const assistantRow = (id: string, text: string, extra: Record<string, unknown> = {}) => ({
  id,
  message_id: id,
  role: 'assistant',
  text,
  timestamp: 1_700_000_042,
  ...extra,
})
