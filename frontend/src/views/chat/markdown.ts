import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import 'highlight.js/styles/github-dark.css'
import { marked } from 'marked'
import { toast } from 'sonner'
import type { MarkdownDep } from './transcript/stream'

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
    return `<code class="math-raw" title="LaTeX formula (not rendered)">${escapeHtml(raw)}</code>`
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
        `<button type="button" class="copy-btn" data-target="${id}" aria-label="Copy code">` +
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
      `<button type="button" class="copy-btn" data-target="${id}" aria-label="Copy code">` +
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

/** Render untrusted assistant Markdown to sanitized terminal-chat HTML. */
export function render(text: string): string {
  if (!text) return ''

  try {
    const { text: stashed, stash } = stashMath(text)
    const parsed = marked.parse(stashed, { breaks: true, gfm: true, async: false })
    let html = String(DOMPurify.sanitize(parsed))
    html = wrapCodeBlocks(html)
    html = markInlineCode(html)
    html = restoreMath(html, stash)
    // The transforms above add only controlled markup, but sanitize the final
    // result as the last operation so every innerHTML call receives clean HTML.
    return String(DOMPurify.sanitize(html))
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
          toast.error('Copy failed. Select the code manually.')
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
