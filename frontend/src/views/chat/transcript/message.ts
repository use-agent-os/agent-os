import { t } from '@/i18n'
import '@/i18n/en/chat'

import { buildCompletionCard } from './acp'
import type { MarkdownDep, TranscriptHeaderStateRef } from './stream'
import { renderedPlainText } from './plainText'
import { modelDisplayName } from './routerFx'
import {
  stripDirectiveTags,
  stripGeneratedArtifactMarkers,
  stripProtocolTextLeak,
  stripTimePrefix,
} from '../logic'

export interface MessageOptions {
  provenanceKind?: string
  provenanceSourceSessionKey?: string
  provenanceSourceTool?: string
  /** Bulk history replay positions the transcript once after every row exists. */
  autoScroll?: boolean
  [key: string]: unknown
}

export interface TurnUsage extends Record<string, unknown> {
  model?: string
  routed_model?: string
  routed_tier?: string
  routing_source?: string
  input_tokens?: number
  inputTokens?: number
  output_tokens?: number
  outputTokens?: number
  cached_tokens?: number
  reasoning_tokens?: number
  cost_usd?: number
  total_savings_usd?: number
  savings_usd?: number
  savings_pct?: number
  total_savings_pct?: number
  routing_confidence?: number
  cache_hit_active?: boolean
}

export interface TurnMeta {
  model: string
  input: number
  output: number
  saved: TurnUsage | null
}

export interface MessageRendererDeps {
  thread: () => HTMLElement | null
  markdown: MarkdownDep
  displayRoleLabel: (role: string) => string
  stampRowMeta: (row: HTMLElement, role: string, timestamp?: string | number | null) => void
  getSessionKey: () => string
  isStreaming: () => boolean
  scrollToBottom: () => void
  /** — shared day cursor + human separator label. */
  dayKey?: (timestamp: string | number | null | undefined) => string
  dayLabel?: (dayKey: string) => string
  /** Shared legacy `_lastHeaderDay` / `_lastHeaderRole` cursor. */
  headerState?: TranscriptHeaderStateRef
  toast: (message: string, kind?: 'info' | 'warn' | 'error', durationMs?: number) => void
  onEdit?: (text: string) => void
  onRegenerate?: (text: string) => void
  /**
   * Build the card for a `subagent_completion` row (transcript/acp.ts). The
   * transcript passes its ACP mounter's, which adds "Open session" and settles
   * the spawn's harness card; absent, a plain completion card is built.
   */
  subagentCard?: (payload: Record<string, unknown>, raw: string) => HTMLElement | null
}

const TURN_META_LS = 'agentos.turnmeta.'

function numeric(value: unknown): number {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n : 0
}

export function formatTokenCount(n: number): string {
  if (!n) return '0'
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${+(n / 1_000).toFixed(1)}k`
  return String(n)
}

export function historyTurnMeta(message: Record<string, unknown>): TurnMeta | null {
  const usage = (message.usage || message.turn_usage || null) as TurnUsage | null
  const model = String(message.model || usage?.model || usage?.routed_model || '')
  const input = numeric(
    message.input ?? message.input_tokens ?? usage?.input_tokens ?? usage?.inputTokens,
  )
  const output = numeric(
    message.output ?? message.output_tokens ?? usage?.output_tokens ?? usage?.outputTokens,
  )
  if (!model && input <= 0 && output <= 0 && !usage) return null
  return {
    model,
    input,
    output,
    saved: usage ? { ...usage, model: usage.model || model || undefined } : null,
  }
}

export function storeTurnMeta(
  sessionKey: string,
  index: number,
  model: string,
  input: number,
  output: number,
  saved: TurnUsage | null,
): void {
  try {
    const key = TURN_META_LS + sessionKey
    const raw = JSON.parse(localStorage.getItem(key) || '[]') as TurnMeta[]
    raw[index] = { model, input, output, saved: saved || null }
    localStorage.setItem(key, JSON.stringify(raw))
  } catch {
    // Storage is an optional history enhancement; live metadata still renders.
  }
}

export function recallTurnMeta(sessionKey: string, index: number): TurnMeta | null {
  try {
    const raw = JSON.parse(localStorage.getItem(TURN_META_LS + sessionKey) || '[]') as TurnMeta[]
    return raw[index] || null
  } catch {
    return null
  }
}

function isSubagentCompletion(role: string, text: string, options: MessageOptions): boolean {
  if (role !== 'system' || !text) return false
  if (options.provenanceSourceTool === 'subagent_completion') return true
  try {
    const payload = JSON.parse(text) as { type?: string }
    return payload?.type === 'subagent_completion'
  } catch {
    return false
  }
}

/** A `subagent_completion` row whose text is not a JSON object: show it raw. */
function appendSubagentDisclosure(body: HTMLElement, text: string): void {
  const details = document.createElement('details')
  details.className = 'chat-subagent-disclosure'
  const summary = document.createElement('summary')
  summary.className = 'chat-subagent-disclosure-summary'
  summary.textContent = t('chat.msgSubagentCompletion')
  const pre = document.createElement('pre')
  pre.className = 'chat-subagent-disclosure-body chat-subagent-disclosure-body--raw'
  pre.textContent = text
  details.append(summary, pre)
  body.appendChild(details)
}

/**
 * How a persisted `system` row reads. A failed turn persists "Error: …"
 * (engine runtime, ACP dispatch): an error. A turn someone stopped persists
 * "Cancelled …" (ACP dispatch): a notice, never the error style.
 */
export function systemRowTone(text: string): 'error' | 'notice' | '' {
  const head = text.trim()
  if (/^error:/i.test(head)) return 'error'
  if (/^cancell?ed\b/i.test(head)) return 'notice'
  return ''
}

function icon(pathData: string[]): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(ns, 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '2')
  svg.setAttribute('stroke-linecap', 'round')
  svg.setAttribute('stroke-linejoin', 'round')
  svg.setAttribute('aria-hidden', 'true')
  pathData.forEach((d) => {
    const path = document.createElementNS(ns, 'path')
    path.setAttribute('d', d)
    svg.appendChild(path)
  })
  return svg
}

function actionButton(action: string, label: string, paths: string[]): HTMLButtonElement {
  const button = document.createElement('button')
  button.type = 'button'
  button.className = 'msg-action'
  button.dataset.action = action
  button.title = label
  button.setAttribute('aria-label', label)
  button.appendChild(icon(paths))
  return button
}

export function createMessageRenderer(deps: MessageRendererDeps) {
  const dayKey = deps.dayKey ?? (() => '')
  const dayLabel = deps.dayLabel ?? ((key: string) => key)
  const headerState = deps.headerState ?? { current: { day: '', role: '' } }
  function extractBubbleText(row: HTMLElement): string {
    const body = row.querySelector(':scope > .msg-body') as HTMLElement | null
    if (!body) return ''
    const attachmentText = body.querySelector('.msg-attachment-text')
    if (attachmentText) return (attachmentText.textContent || '').trim()
    const clone = body.cloneNode(true) as HTMLElement
    // `.thinking-block` is the collapsible reasoning disclosure the history and
    // stream renderers prepend inside `.msg-body`; its summary label and lazily
    // fetched body are chrome, not reply text, so they never reach the clipboard.
    // `.code-block-header` holds a fenced block's language label and its own
    // Copy button, which would otherwise land above the code.
    clone
      .querySelectorAll('.msg-actions, .msg-meta, .thinking-block, .code-block-header')
      .forEach((node) => node.remove())
    // Assistant bubbles are rendered markdown, where line breaks live in `<br>`
    // and block tags that `textContent` drops; other rows hold their text
    // verbatim under `white-space: pre-wrap`.
    return renderedPlainText(clone, !row.classList.contains('assistant'))
  }

  function copyText(text: string): Promise<void> {
    if (!text) return Promise.resolve()
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.left = '-9999px'
    document.body.appendChild(textarea)
    textarea.select()
    let copied = false
    try {
      copied = document.execCommand('copy')
    } finally {
      textarea.remove()
    }
    return copied ? Promise.resolve() : Promise.reject(new Error(t('chat.msgCopyFailedError')))
  }

  function attachHoverActions(row: HTMLElement, role: string): void {
    if (!['user', 'assistant'].includes(role)) return
    const body = row.querySelector(':scope > .msg-body') as HTMLElement | null
    if (!body) return
    body.querySelector(':scope > .msg-actions')?.remove()
    const actions = document.createElement('div')
    actions.className = 'msg-actions'
    actions.setAttribute('role', 'toolbar')
    actions.setAttribute(
      'aria-label',
      role === 'user' ? t('chat.msgUserActions') : t('chat.msgAgentActions'),
    )
    actions.appendChild(
      actionButton('copy', t('chat.msgCopy'), [
        'M9 9h11a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2V9Z',
        'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
      ]),
    )
    if (role === 'assistant' && deps.onRegenerate) {
      actions.appendChild(
        actionButton('regenerate', t('chat.msgRegenerate'), [
          'M20 11a8.1 8.1 0 1 1-2.3-5.7L20 8',
          'M20 4v4h-4',
        ]),
      )
    }
    if (role === 'user' && deps.onEdit) {
      actions.appendChild(
        actionButton('edit', t('chat.msgEdit'), [
          'M12 20h9',
          'M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z',
        ]),
      )
    }
    actions.addEventListener('click', (event) => {
      const button = (event.target as Element).closest<HTMLButtonElement>('.msg-action')
      if (!button) return
      event.preventDefault()
      event.stopPropagation()
      const action = button.dataset.action
      if (action === 'copy') {
        void copyText(extractBubbleText(row))
          .then(() => deps.toast(t('chat.msgCopied'), 'info', 1200))
          .catch((error: unknown) =>
            deps.toast(
              t('chat.msgCopyFailed', {
                message: error instanceof Error ? error.message : String(error),
              }),
              'error',
              2500,
            ),
          )
        return
      }
      if (deps.isStreaming()) {
        deps.toast(t('chat.msgWaitForResponse'), 'warn', 2000)
        return
      }
      if (action === 'edit' && deps.onEdit) {
        const text = extractBubbleText(row)
        let current: Element | null = row
        while (current) {
          const next: Element | null = current.nextElementSibling
          current.remove()
          current = next
        }
        deps.onEdit(text)
      } else if (action === 'regenerate' && deps.onRegenerate) {
        let user = row.previousElementSibling as HTMLElement | null
        while (user && !user.matches('.msg.user'))
          user = user.previousElementSibling as HTMLElement | null
        if (!user) {
          deps.toast(t('chat.msgNoPrevious'), 'info', 2000)
          return
        }
        const text = extractBubbleText(user)
        let current: Element | null = user.nextElementSibling
        while (current) {
          const next: Element | null = current.nextElementSibling
          current.remove()
          current = next
        }
        deps.onRegenerate(text)
      }
    })
    body.appendChild(actions)
  }

  function renderBody(
    body: HTMLElement,
    role: string,
    text: string,
    options: MessageOptions = {},
  ): void {
    body.className = 'msg-body'
    body.textContent = ''
    delete body.dataset.tone
    const subagent = isSubagentCompletion(role, text, options)
    const visibleText = role === 'assistant' ? stripGeneratedArtifactMarkers(text) : text
    if (role === 'assistant' && visibleText) {
      body.innerHTML = deps.markdown.render(stripProtocolTextLeak(stripDirectiveTags(visibleText)))
      deps.markdown.bindCopy(body)
      deps.markdown.bindHighlight?.(body)
    } else if (subagent) {
      appendSubagentCompletion(body, visibleText)
    } else if (visibleText) {
      body.textContent = role === 'user' ? stripTimePrefix(visibleText) : visibleText
      if (role === 'system') {
        const tone = systemRowTone(visibleText)
        if (tone) body.dataset.tone = tone
      }
    }
  }

  function appendSubagentCompletion(body: HTMLElement, text: string): void {
    let payload: Record<string, unknown> | null = null
    try {
      const parsed = JSON.parse(text) as unknown
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        payload = parsed as Record<string, unknown>
      }
    } catch {
      payload = null
    }
    if (!payload) {
      appendSubagentDisclosure(body, text)
      return
    }
    const card =
      deps.subagentCard?.(payload, text) ??
      buildCompletionCard(payload, { markdown: deps.markdown, raw: text })
    body.appendChild(card)
  }

  function messageTimestampTitle(timestamp?: string | number | null): string {
    if (timestamp == null || timestamp === '') return ''
    const date =
      typeof timestamp === 'number'
        ? new Date(timestamp)
        : /^\d+$/.test(timestamp.trim())
          ? new Date(Number(timestamp))
          : new Date(timestamp)
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
  }

  /**
   * reconcile a row header after history identity reuse.
   * `sameGroup` is calculated by the history traversal rather than from the
   * current DOM, because unconsumed rows and a live stream tail remain mounted
   * while pagination is being rebuilt.
   */
  function syncMessageHeader(
    row: HTMLElement,
    displayRole: string,
    timestamp: string | number | null | undefined,
    options: MessageOptions = {},
    sameGroup = false,
  ): void {
    const existing = row.querySelector<HTMLElement>(':scope > .msg-header')
    const timestampTitle = messageTimestampTitle(timestamp)
    if (sameGroup) {
      existing?.remove()
      if (timestampTitle) row.title = timestampTitle
      else row.removeAttribute('title')
      return
    }

    const header = existing || document.createElement('div')
    header.className = 'msg-header'
    header.replaceChildren()

    const label = document.createElement('span')
    label.className = 'role-label'
    label.textContent = deps.displayRoleLabel(displayRole)
    header.appendChild(label)

    if (options.provenanceKind === 'cron') {
      const tags = document.createElement('span')
      tags.className = 'msg-tags'
      const tag = document.createElement('span')
      tag.className = 'cron-tag'
      tag.textContent = t('chat.msgCronTag')
      tags.appendChild(tag)
      header.appendChild(tags)
    }

    const time = document.createElement('span')
    time.className = 'msg-time'
    time.textContent = row.dataset.time || ''
    header.appendChild(time)
    if (timestampTitle) header.title = timestampTitle
    else header.removeAttribute('title')
    row.removeAttribute('title')
    if (!existing) row.insertBefore(header, row.firstChild)
  }

  function addMessage(
    role: string,
    text: string,
    timestamp?: string | number | null,
    options: MessageOptions = {},
  ): HTMLElement | null {
    const thread = deps.thread()
    if (!thread) return null
    thread.querySelector('.chat-empty')?.remove()

    // every non-stream row participates in the SAME day
    // cursor as history and streaming. This keeps a live prompt on the correct
    // side of midnight and prevents the stream from inserting a separator
    // between that prompt and its response.
    const day = dayKey(timestamp)
    if (day && day !== headerState.current.day) {
      const separator = document.createElement('div')
      separator.className = 'chat-day-sep'
      const label = document.createElement('span')
      label.textContent = dayLabel(day)
      separator.appendChild(label)
      thread.appendChild(separator)
      headerState.current.day = day
      headerState.current.role = ''
    }

    const subagent = isSubagentCompletion(role, text, options)
    const displayRole = subagent ? 'subagent' : role
    const row = document.createElement('div')
    row.className = `msg ${displayRole}`
    row.dataset.historyRole = role
    deps.stampRowMeta(row, displayRole, timestamp)

    const collapsible = displayRole === 'user' || displayRole === 'assistant'
    const sameGroup =
      collapsible &&
      displayRole === headerState.current.role &&
      day === headerState.current.day &&
      day !== ''
    if (collapsible) headerState.current.role = displayRole
    syncMessageHeader(row, displayRole, timestamp, options, sameGroup)

    const body = document.createElement('div')
    renderBody(body, role, text, options)
    row.appendChild(body)
    attachHoverActions(row, displayRole)
    thread.appendChild(row)
    if (options.autoScroll !== false) deps.scrollToBottom()
    return row
  }

  function attachTurnMeta(
    row: HTMLElement | null,
    model: string,
    totalInput: number,
    totalOutput: number,
    usage: TurnUsage | null = null,
  ): void {
    if (!row) return
    row.querySelectorAll(':scope > .msg-meta').forEach((node) => node.remove())
    const data = usage || {}
    const hasModel = Boolean(model.trim())
    const hasTokens = totalInput > 0 || totalOutput > 0
    if (!hasModel && !hasTokens && numeric(data.cost_usd) <= 0) return
    const meta = document.createElement('div')
    meta.className = 'msg-meta'
    const add = (className: string, text: string, title = ''): HTMLSpanElement => {
      const span = document.createElement('span')
      span.className = className
      span.textContent = text
      if (title) span.title = title
      meta.appendChild(span)
      return span
    }
    if (hasModel) {
      const display = modelDisplayName(model)
      add('msg-meta__model', display, display !== model ? model : '')
    }
    if (hasTokens) {
      add(
        'msg-meta__tokens',
        `↑${formatTokenCount(totalInput)} ↓${formatTokenCount(totalOutput)}`,
        `Turn — input: ${totalInput.toLocaleString()}, output: ${totalOutput.toLocaleString()} tokens`,
      )
    }
    const cached = numeric(data.cached_tokens)
    if (cached > 0) add('msg-meta__cached', `cache:${formatTokenCount(cached)}`)
    const reasoning = numeric(data.reasoning_tokens)
    if (reasoning > 0) add('msg-meta__reasoning', `think:${formatTokenCount(reasoning)}`)
    const cost = numeric(data.cost_usd)
    if (cost > 0) add('msg-meta__cost', `$${cost.toFixed(6).replace(/\.?0+$/, '')}`)
    row.appendChild(meta)
  }

  return {
    addMessage,
    renderBody,
    attachHoverActions,
    attachTurnMeta,
    extractBubbleText,
    syncMessageHeader,
    resetGrouping: () => {
      headerState.current.day = ''
      headerState.current.role = ''
    },
  }
}

export type MessageRenderer = ReturnType<typeof createMessageRenderer>
