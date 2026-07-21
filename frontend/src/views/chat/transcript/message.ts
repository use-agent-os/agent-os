import type { MarkdownDep } from './stream'
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
  total_savings_pct?: number
  __savings_ui_suppressed?: boolean
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
  toast: (message: string, kind?: 'info' | 'warn' | 'error', durationMs?: number) => void
  onEdit?: (text: string) => void
  onRegenerate?: (text: string) => void
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

function appendSubagentDisclosure(body: HTMLElement, text: string): void {
  const details = document.createElement('details')
  details.className = 'chat-subagent-disclosure'
  const summary = document.createElement('summary')
  summary.className = 'chat-subagent-disclosure-summary'
  const pre = document.createElement('pre')
  pre.className = 'chat-subagent-disclosure-body'
  try {
    const payload = JSON.parse(text) as { child_session_key?: string; session_key?: string }
    summary.textContent = `Subagent: ${payload.child_session_key || payload.session_key || 'completion'}`
    pre.textContent = JSON.stringify(payload, null, 2)
  } catch {
    summary.textContent = 'Subagent completion'
    pre.classList.add('chat-subagent-disclosure-body--raw')
    pre.textContent = text
  }
  details.append(summary, pre)
  body.appendChild(details)
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
  function extractBubbleText(row: HTMLElement): string {
    const body = row.querySelector(':scope > .msg-body') as HTMLElement | null
    if (!body) return ''
    const attachmentText = body.querySelector('.msg-attachment-text')
    if (attachmentText) return (attachmentText.textContent || '').trim()
    const clone = body.cloneNode(true) as HTMLElement
    clone.querySelectorAll('.msg-actions, .msg-meta').forEach((node) => node.remove())
    return (clone.textContent || '').trim()
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
    return copied ? Promise.resolve() : Promise.reject(new Error('Copy failed'))
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
      role === 'user' ? 'User message actions' : 'Agent message actions',
    )
    actions.appendChild(
      actionButton('copy', 'Copy message', [
        'M9 9h11a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2V9Z',
        'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
      ]),
    )
    if (role === 'assistant' && deps.onRegenerate) {
      actions.appendChild(
        actionButton('regenerate', 'Regenerate response', [
          'M20 11a8.1 8.1 0 1 1-2.3-5.7L20 8',
          'M20 4v4h-4',
        ]),
      )
    }
    if (role === 'user' && deps.onEdit) {
      actions.appendChild(
        actionButton('edit', 'Edit message', [
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
          .then(() => deps.toast('Copied', 'info', 1200))
          .catch((error: unknown) =>
            deps.toast(
              'Copy failed: ' + (error instanceof Error ? error.message : String(error)),
              'error',
              2500,
            ),
          )
        return
      }
      if (deps.isStreaming()) {
        deps.toast('Wait for the current response to finish', 'warn', 2000)
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
          deps.toast('No previous message to regenerate', 'info', 2000)
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
    const subagent = isSubagentCompletion(role, text, options)
    const visibleText = role === 'assistant' ? stripGeneratedArtifactMarkers(text) : text
    if (role === 'assistant' && visibleText) {
      body.innerHTML = deps.markdown.render(stripProtocolTextLeak(stripDirectiveTags(visibleText)))
      deps.markdown.bindCopy(body)
      deps.markdown.bindHighlight?.(body)
    } else if (subagent) {
      appendSubagentDisclosure(body, visibleText)
    } else if (visibleText) {
      body.textContent = role === 'user' ? stripTimePrefix(visibleText) : visibleText
    }
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
    const subagent = isSubagentCompletion(role, text, options)
    const displayRole = subagent ? 'subagent' : role
    const row = document.createElement('div')
    row.className = `msg ${displayRole}`
    row.dataset.historyRole = role
    deps.stampRowMeta(row, displayRole, timestamp)

    const collapsible = displayRole === 'user' || displayRole === 'assistant'
    const previous = thread.lastElementChild
    const sameGroup =
      collapsible &&
      previous?.classList.contains('msg') === true &&
      previous.classList.contains(displayRole)
    if (!sameGroup) {
      const header = document.createElement('div')
      header.className = 'msg-header'
      const label = document.createElement('span')
      label.className = 'role-label'
      label.textContent = deps.displayRoleLabel(displayRole)
      header.appendChild(label)
      if (options.provenanceKind === 'cron') {
        const tags = document.createElement('span')
        tags.className = 'msg-tags'
        const tag = document.createElement('span')
        tag.className = 'cron-tag'
        tag.textContent = 'Cron'
        tags.appendChild(tag)
        header.appendChild(tags)
      }
      const time = document.createElement('span')
      time.className = 'msg-time'
      time.textContent = row.dataset.time || ''
      header.appendChild(time)
      row.appendChild(header)
    }

    const body = document.createElement('div')
    renderBody(body, role, text, options)
    row.appendChild(body)
    attachHoverActions(row, displayRole)
    thread.appendChild(row)
    deps.scrollToBottom()
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
    const savings = numeric(data.total_savings_pct)
    const hasSavings =
      !data.__savings_ui_suppressed &&
      Boolean(data.routed_tier && data.routing_source && data.routing_source !== 'none') &&
      savings > 0
    if (!hasModel && !hasTokens && numeric(data.cost_usd) <= 0 && !hasSavings) return
    const meta = document.createElement('div')
    meta.className = 'msg-meta'
    const add = (className: string, text: string, title = '') => {
      const span = document.createElement('span')
      span.className = className
      span.textContent = text
      if (title) span.title = title
      meta.appendChild(span)
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
    if (hasSavings) {
      add(
        'msg-meta__saved',
        `saved ~${Math.round(savings)}%`,
        `Pilot Router routed this turn (~${Math.round(savings)}% vs flagship)`,
      )
    }
    row.appendChild(meta)
  }

  return {
    addMessage,
    renderBody,
    attachHoverActions,
    attachTurnMeta,
    extractBubbleText,
    resetGrouping: () => {},
  }
}

export type MessageRenderer = ReturnType<typeof createMessageRenderer>
