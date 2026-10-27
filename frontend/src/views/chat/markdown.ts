import { t } from '@/i18n'
import '@/i18n/en/chat'

import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import typescript from 'highlight.js/lib/languages/typescript'
import yaml from 'highlight.js/lib/languages/yaml'
import 'highlight.js/styles/github-dark.css'
import { Marked } from 'marked'
import { toast } from 'sonner'
import type { MarkdownDep } from './transcript/stream'
import { qrDataUrl } from '@/lib/qr'

// Import the lightweight core plus the languages used in AgentOS transcripts.
// Importing `highlight.js` registers every bundled grammar and adds roughly a
// megabyte to the first Chat route download.
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('python', python)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('yaml', yaml)

// Ported from static/js/markdown.js:11-37. Code spans win at the same position,
// so dollar signs inside code are never interpreted as LaTeX-ish content.
const MATH_SCAN =
  /(```[\s\S]*?```|`[^`\n]+?`|\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([^)\n]+?\\\)|\$(?![\s\d])(?:\\\$|[^$\n])+?(?<![\s])\$)/g
const MATH_SENTINEL = /M(\d+)/g

let codeBlockId = 0

function escapeHtml(text: string): string {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function nextCodeBlockId(): string {
  codeBlockId += 1
  return `chat-code-${codeBlockId.toString(36)}`
}

function stashMath(text: string): { text: string; stash: string[] } {
  const stash: string[] = []
  const stashedText = text.replace(MATH_SCAN, (match) => {
    if (match.startsWith('```') || (match.startsWith('`') && !match.startsWith('$'))) {
      return match
    }
    const index = stash.length
    stash.push(match)
    return `M${index}`
  })
  return { text: stashedText, stash }
}

function restoreMath(html: string, stash: string[]): string {
  if (stash.length === 0) return html
  return html.replace(MATH_SENTINEL, (_match, rawIndex: string) => {
    const raw = stash[Number(rawIndex)]
    if (raw === undefined) return ''
    return `<code class="math-raw" title="${escapeHtml(t('chat.mathTitle'))}">${escapeHtml(raw)}</code>`
  })
}

function wrapCodeBlocks(html: string): string {
  return html.replace(
    /<pre><code(?: class="language-([\w-]+)")?>([\s\S]*?)<\/code><\/pre>/g,
    (_match, rawLanguage: string | undefined, code: string) => {
      const id = nextCodeBlockId()
      const language = rawLanguage || ''
      const languageClass = language ? ` language-${language}` : ''
      return (
        '<div class="code-block">' +
        '<div class="code-block-header">' +
        `<span class="code-lang">${language}</span>` +
        `<button type="button" class="copy-btn" data-target="${id}" aria-label="${escapeHtml(t('chat.copyCode'))}">` +
        '<span aria-hidden="true">⧉</span> Copy</button>' +
        '</div>' +
        `<pre id="${id}"><code class="code-content${languageClass}">${code}</code></pre>` +
        '</div>'
      )
    },
  )
}

function markInlineCode(html: string): string {
  // Fenced blocks have a `code-content` class after wrapCodeBlocks, so the only
  // remaining bare code tags are inline spans emitted by marked.
  return html.replace(/<code>([\s\S]*?)<\/code>/g, '<code class="inline-code">$1</code>')
}

// Ported from static/js/markdown.js:67-83. Every user-controlled byte is escaped
// before the small formatting pass, so this remains safe if parsing fails.
function fallbackRender(text: string): string {
  let html = escapeHtml(text)
  html = html.replace(/```([\w-]*)\n([\s\S]*?)```/g, (_match, language: string, code: string) => {
    const id = nextCodeBlockId()
    const languageClass = language ? ` language-${language}` : ''
    return (
      '<div class="code-block">' +
      '<div class="code-block-header">' +
      `<span class="code-lang">${language}</span>` +
      `<button type="button" class="copy-btn" data-target="${id}" aria-label="${escapeHtml(t('chat.copyCode'))}">` +
      '<span aria-hidden="true">⧉</span> Copy</button>' +
      '</div>' +
      `<pre id="${id}"><code class="code-content${languageClass}">${code}</code></pre>` +
      '</div>'
    )
  })
  html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>')
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>')
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>')
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>')
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>')
  html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>')
  return html.replace(/\n\n/g, '<br><br>')
}

/**
 * `![Receive](agentos-qr:0x89E0…)` — a QR the assistant wants shown, drawn in
 * this process and inlined as a `data:` image.
 *
 * This exists because the alternative is what an assistant reaches for on its
 * own: a public QR web service, with the address in the query string. That
 * hands a third party the very thing being displayed, and it does not even
 * work — the desktop window's CSP allows no arbitrary remote image host, so it
 * renders as a broken icon. The scheme is the sanctioned way to ask, and it is
 * substituted BEFORE parsing because the sanitizer would strip an unknown URL
 * scheme off the `<img>` long before any later pass could see it.
 */
const QR_IMAGE = /!\[([^\]\n]*)\]\(\s*agentos-qr:([^)\s]+)\s*\)/g
/** Roughly a version-25 symbol. Past this a QR is unreadable on a screen. */
const QR_MAX_CHARS = 512

function inlineQrImages(text: string): string {
  if (!text.includes('agentos-qr:')) return text
  return text.replace(QR_IMAGE, (whole, alt: string, payload: string) => {
    let data = payload
    try {
      data = decodeURIComponent(payload)
    } catch {
      // A stray `%` is not an encoding; take the bytes as written.
    }
    if (!data || data.length > QR_MAX_CHARS) return whole
    try {
      return `![${alt}](${qrDataUrl(data)})`
    } catch {
      // An payload the encoder refuses stays as written rather than vanishing.
      return whole
    }
  })
}

/**
 * Replace a remote image with a link to it.
 *
 * An `<img src="https://…">` in assistant markdown is a request the browser
 * makes without asking: whoever hosts it learns that this conversation reached
 * that URL, and anything the model put in the path or query — a wallet
 * address, say — goes with it. The desktop CSP blocks the load anyway and
 * leaves a broken icon, which reads as a bug rather than as a refusal. A link
 * keeps the image reachable on a deliberate click and says who would serve it.
 *
 * `data:` and `blob:` images are untouched: they are already in hand, and they
 * are how the transcript shows attachments and locally drawn QRs.
 */
function linkRemoteImages(html: string): string {
  const template = document.createElement('template')
  template.innerHTML = html
  for (const image of template.content.querySelectorAll('img[src]')) {
    const src = image.getAttribute('src') || ''
    if (!/^https?:/i.test(src)) continue
    let host = src
    try {
      host = new URL(src).host
    } catch {
      // Not parseable: show the raw src rather than claiming a host.
    }
    const note = template.ownerDocument.createElement('span')
    note.className = 'msg-extimg'
    note.title = t('chat.externalImageTitle')
    note.append(`${t('chat.externalImageBlocked')} `)
    const link = template.ownerDocument.createElement('a')
    link.setAttribute('href', src)
    link.setAttribute('target', '_blank')
    link.setAttribute('rel', 'noreferrer noopener')
    link.textContent = image.getAttribute('alt') || host
    note.appendChild(link)
    image.replaceWith(note)
  }
  return template.innerHTML
}

/**
 * Send external links to a new tab.
 *
 * The transcript is a long-lived surface: following a link in place unmounts
 * the chat and loses the scroll position and any in-flight turn. Assistant
 * markdown is untrusted, so this runs before the final sanitize pass and
 * touches only http(s) hrefs — in-page anchors and app-relative paths keep
 * their default behaviour.
 */
function openExternalLinksInNewTab(html: string): string {
  const template = document.createElement('template')
  template.innerHTML = html
  for (const anchor of template.content.querySelectorAll('a[href]')) {
    if (!/^https?:/i.test(anchor.getAttribute('href') || '')) {
      // Anything the model wrote itself is not trusted to pick a browsing
      // context — only the branch below gets to set one.
      anchor.removeAttribute('target')
      continue
    }
    anchor.setAttribute('target', '_blank')
    // noopener also blunts reverse-tabnabbing from a link the model wrote.
    anchor.setAttribute('rel', 'noreferrer noopener')
  }
  return template.innerHTML
}

// Raw HTML passes through the sanitizer, which drops tags it does not know.
// A tool call the model wrote as text (<invoke>, <tool_call>, ...) reaches the
// console only when it never ran, so its tags must stay readable instead of
// vanishing: render them as literal text. Code spans and fences never produce
// html tokens, so they are untouched.
const TEXT_TOOL_CALL_TAG_RE =
  /<\/?\s*(?:invoke|parameter|tool_calls?|tvoe_calls|function|minimax:tool_call)(?![\w-])/i

const markdown = new Marked({
  renderer: {
    html({ text }) {
      if (!TEXT_TOOL_CALL_TAG_RE.test(text)) return text
      return escapeHtml(text).replace(/\n/g, '<br>')
    },
  },
})

/** Render untrusted assistant Markdown to sanitized terminal-chat HTML. */
export function render(text: string): string {
  if (!text) return ''

  try {
    const { text: stashed, stash } = stashMath(inlineQrImages(text))
    const parsed = markdown.parse(stashed, { breaks: true, gfm: true, async: false })
    let html = String(DOMPurify.sanitize(parsed))
    html = wrapCodeBlocks(html)
    html = markInlineCode(html)
    html = restoreMath(html, stash)
    html = linkRemoteImages(html)
    html = openExternalLinksInNewTab(html)
    // The transforms above add only controlled markup, but sanitize the final
    // result as the last operation so every innerHTML call receives clean HTML.
    // `target` is allowed through because the pass above is what sets it, and
    // strips any the model tried to choose for itself.
    return String(DOMPurify.sanitize(html, { ADD_ATTR: ['target'] }))
  } catch {
    return fallbackRender(text)
  }
}

function focusWithoutScroll(element: HTMLElement): void {
  try {
    element.focus({ preventScroll: true })
  } catch {
    element.focus()
  }
}

function copyTextFallback(text: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!document.body || typeof document.execCommand !== 'function') {
      reject(new Error('Clipboard API unavailable'))
      return
    }

    const textarea = document.createElement('textarea')
    const activeElement = document.activeElement as HTMLElement | null
    textarea.value = text
    textarea.setAttribute('readonly', '')
    Object.assign(textarea.style, {
      position: 'fixed',
      top: '0',
      left: '0',
      width: '1px',
      height: '1px',
      opacity: '0',
      pointerEvents: 'none',
    })

    document.body.appendChild(textarea)
    focusWithoutScroll(textarea)
    textarea.select()
    textarea.setSelectionRange(0, textarea.value.length)

    let copied = false
    try {
      copied = document.execCommand('copy')
    } catch (error) {
      reject(error)
      return
    } finally {
      textarea.remove()
      if (activeElement && typeof activeElement.focus === 'function') {
        focusWithoutScroll(activeElement)
      }
    }

    if (copied) resolve()
    else reject(new Error('Copy command rejected'))
  })
}

function copyText(text: string): Promise<void> {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    return navigator.clipboard.writeText(text).catch(() => copyTextFallback(text))
  }
  return copyTextFallback(text)
}

function showCopyStatus(button: HTMLButtonElement, label: string, duration = 1500): void {
  if (!button.dataset.copyOriginalHtml) button.dataset.copyOriginalHtml = button.innerHTML
  const previousTimer = Number(button.dataset.copyStatusTimer || 0)
  if (previousTimer) window.clearTimeout(previousTimer)

  button.textContent = label
  const timer = window.setTimeout(() => {
    button.innerHTML = button.dataset.copyOriginalHtml || '⧉ Copy'
    delete button.dataset.copyOriginalHtml
    delete button.dataset.copyStatusTimer
  }, duration)
  button.dataset.copyStatusTimer = String(timer)
}

/** Bind copy controls generated by render(). */
export function bindCopy(container: HTMLElement): void {
  container.querySelectorAll<HTMLButtonElement>('.copy-btn').forEach((button) => {
    if (button.dataset.copyBound === 'true') return
    button.dataset.copyBound = 'true'
    button.addEventListener('click', () => {
      const targetId = button.dataset.target
      const target = targetId ? document.getElementById(targetId) : null
      if (!target) return

      button.disabled = true
      void copyText(target.textContent || '')
        .then(() => showCopyStatus(button, '✓ Copied'))
        .catch(() => {
          showCopyStatus(button, '! Failed', 1800)
          toast.error(t('chat.copyFailed'))
        })
        .finally(() => {
          button.disabled = false
        })
    })
  })
}

/** Apply syntax highlighting after the sanitized HTML has entered the DOM. */
export function bindHighlight(container: HTMLElement): void {
  container.querySelectorAll<HTMLElement>('pre code').forEach((code) => {
    try {
      hljs.highlightElement(code)
    } catch {
      // Unknown language aliases degrade to readable, unhighlighted code.
    }
  })
}

export const chatMarkdown: MarkdownDep = { render, bindCopy, bindHighlight }
