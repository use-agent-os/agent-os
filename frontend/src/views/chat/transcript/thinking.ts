import { t } from '@/i18n'
import '@/i18n/en/chat'

/**
 * Collapsible model-reasoning block shared by the live stream (open while the
 * model thinks, collapsed once reply text starts) and history rows (collapsed,
 * body fetched on first expand via `chat.thinking`).
 *
 * Reasoning renders as plain text (`textContent`) on purpose: it is untrusted
 * model output and never goes through the markdown/innerHTML pipeline.
 */
export interface ThinkingBlockParts {
  details: HTMLDetailsElement
  content: HTMLElement
}

export function createThinkingBlock(opts: { open: boolean }): ThinkingBlockParts {
  const details = document.createElement('details')
  details.className = 'thinking-block'
  details.open = opts.open
  const summary = document.createElement('summary')
  summary.className = 'thinking-block-summary'
  summary.textContent = t('chat.thinkingBlockLabel')
  const content = document.createElement('div')
  content.className = 'thinking-block-content'
  details.appendChild(summary)
  details.appendChild(content)
  return { details, content }
}

/** History rows: resolve the reasoning body from the gateway on first expand. */
export function wireLazyThinkingBlock(
  parts: ThinkingBlockParts,
  fetchThinking: () => Promise<string | null>,
): void {
  let requested = false
  parts.details.addEventListener('toggle', () => {
    if (!parts.details.open || requested) return
    requested = true
    parts.content.textContent = t('chat.thinkingBlockLoading')
    void fetchThinking()
      .then((reasoning) => {
        parts.content.textContent = reasoning || t('chat.thinkingBlockEmpty')
      })
      .catch(() => {
        requested = false
        parts.content.textContent = t('chat.thinkingBlockError')
      })
  })
}
