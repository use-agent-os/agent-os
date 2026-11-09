import DOMPurify, { type Config } from 'dompurify'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import typescript from 'highlight.js/lib/languages/typescript'
import yaml from 'highlight.js/lib/languages/yaml'
import { Marked } from 'marked'
import { useCallback, useMemo, type MouseEvent } from 'react'
import { toast } from 'sonner'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { qrDataUrl } from '@/lib/qr'
import { cn } from '@/lib/utils'
import '@/web/styles/transcript.css'

// Chat Markdown (overhaul-control-ui D7, chat spec "Markdown is sanitized"):
// marked -> DOMPurify with an explicit allowlist -> controlled post-passes ->
// a final sanitize. highlight.js runs on the code text inside marked's
// renderer, so the highlighted markup goes through the sanitizer too, and
// its colours come from theme tokens (web/styles/transcript.css).

hljs.registerLanguage('bash', bash)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('python', python)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('yaml', yaml)

const ALLOWED_TAGS = [
  'a',
  'abbr',
  'b',
  'blockquote',
  'br',
  'button',
  'code',
  'del',
  'details',
  'div',
  'em',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'i',
  'img',
  'input',
  'kbd',
  'li',
  'ol',
  'p',
  'pre',
  's',
  'span',
  'strong',
  'sub',
  'summary',
  'sup',
  'table',
  'tbody',
  'td',
  'tfoot',
  'th',
  'thead',
  'tr',
  'ul',
]
const ALLOWED_ATTR = [
  'href',
  'title',
  'alt',
  'src',
  'class',
  'colspan',
  'rowspan',
  'align',
  'type',
  'checked',
  'disabled',
  'data-copy',
  'aria-label',
  'target',
  'rel',
  'start',
]
/** http(s), mailto, in-app relative/anchor links, and inline data/blob images. */
const ALLOWED_URI =
  /^(?:(?:https?|mailto):|data:image\/(?:png|gif|jpeg|webp);|blob:|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i

const PURIFY: Config = {
  ALLOWED_TAGS,
  ALLOWED_ATTR,
  ALLOWED_URI_REGEXP: ALLOWED_URI,
  ALLOW_DATA_ATTR: false,
  FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form', 'svg', 'math'],
  FORBID_ATTR: ['style'],
}

function escapeHtml(text: string): string {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// Code spans win at the same position, so `$` inside code is never math.
const MATH_SCAN =
  /(```[\s\S]*?```|`[^`\n]+?`|\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([^)\n]+?\\\)|\$(?![\s\d])(?:\\\$|[^$\n])+?(?<![\s])\$)/g
const MATH_SENTINEL = /M(\d+)/g

function stashMath(text: string): { text: string; stash: string[] } {
  const stash: string[] = []
  const out = text.replace(MATH_SCAN, (match) => {
    if (match.startsWith('`')) return match
    stash.push(match)
    return `M${stash.length - 1}`
  })
  return { text: out, stash }
}

function restoreMath(html: string, stash: string[]): string {
  if (stash.length === 0) return html
  return html.replace(MATH_SENTINEL, (_m, i: string) => {
    const raw = stash[Number(i)]
    if (raw === undefined) return ''
    return `<code class="math-raw" title="${escapeHtml(t('webChat.mathTitle'))}">${escapeHtml(raw)}</code>`
  })
}

/** `![alt](agentos-qr:payload)` -> a locally drawn QR `data:` image. */
const QR_IMAGE = /!\[([^\]\n]*)\]\(\s*agentos-qr:([^)\s]+)\s*\)/g
function inlineQrImages(text: string): string {
  if (!text.includes('agentos-qr:')) return text
  return text.replace(QR_IMAGE, (whole, alt: string, payload: string) => {
    let data = payload
    try {
      data = decodeURIComponent(payload)
    } catch {
      /* take the bytes as written */
    }
    if (!data || data.length > 512) return whole
    try {
      return `![${alt}](${qrDataUrl(data)})`
    } catch {
      return whole
    }
  })
}

// Tool-call tags the model wrote as text stay readable instead of vanishing.
const TEXT_TOOL_CALL_TAG =
  /<\/?\s*(?:invoke|parameter|tool_calls?|tvoe_calls|function|minimax:tool_call)(?![\w-])/i

function highlight(code: string, lang: string): string {
  const language = lang && hljs.getLanguage(lang) ? lang : null
  try {
    return language ? hljs.highlight(code, { language }).value : escapeHtml(code)
  } catch {
    return escapeHtml(code)
  }
}

const marked = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    html({ text }) {
      if (!TEXT_TOOL_CALL_TAG.test(text)) return text
      return escapeHtml(text).replace(/\n/g, '<br>')
    },
    code({ text, lang }) {
      const language = (lang || '').trim().split(/\s+/)[0]?.toLowerCase() ?? ''
      const safeLang = /^[\w+-]{1,32}$/.test(language) ? language : ''
      const languageClass = safeLang ? ` language-${safeLang}` : ''
      return (
        '<div class="code-block">' +
        '<div class="code-block-header">' +
        `<span>${escapeHtml(safeLang)}</span>` +
        `<button type="button" class="copy-btn" data-copy="code" aria-label="${escapeHtml(t('webChat.copyCode'))}">${escapeHtml(t('webChat.copy'))}</button>` +
        '</div>' +
        `<pre><code class="hljs${languageClass}">${highlight(text, safeLang)}</code></pre>` +
        '</div>'
      )
    },
  },
})

/** Wide tables scroll inside their own container. */
function wrapTables(root: DocumentFragment): void {
  for (const table of root.querySelectorAll('table')) {
    const wrapper = root.ownerDocument!.createElement('div')
    wrapper.className = 'table-scroll'
    table.replaceWith(wrapper)
    wrapper.appendChild(table)
  }
}

/** Remote images become a link: loading them leaks the conversation to the host. */
function linkRemoteImages(root: DocumentFragment): void {
  for (const image of root.querySelectorAll('img[src]')) {
    const src = image.getAttribute('src') || ''
    // DOMPurify lets any data: URI through on <img>; only raster images may stay.
    if (/^data:/i.test(src) && !/^data:image\/(?:png|gif|jpeg|webp);/i.test(src)) {
      image.removeAttribute('src')
      continue
    }
    if (!/^https?:/i.test(src)) continue
    let host = src
    try {
      host = new URL(src).host
    } catch {
      /* show the raw src */
    }
    const note = root.ownerDocument!.createElement('span')
    note.className = 'ext-image'
    note.title = t('webChat.externalImageTitle')
    note.append(`${t('webChat.externalImage')} `)
    const link = root.ownerDocument!.createElement('a')
    link.setAttribute('href', src)
    link.textContent = image.getAttribute('alt') || host
    note.appendChild(link)
    image.replaceWith(note)
  }
}

/**
 * Links to another origin open in a new tab with noopener/noreferrer; any
 * `target` the model chose itself is removed.
 */
function routeLinks(root: DocumentFragment, origin: string): void {
  for (const anchor of root.querySelectorAll('a[href]')) {
    anchor.removeAttribute('target')
    anchor.removeAttribute('rel')
    const href = anchor.getAttribute('href') || ''
    if (!/^https?:/i.test(href)) continue
    let external = true
    try {
      external = new URL(href).origin !== origin
    } catch {
      /* unparsable: treat as external */
    }
    if (!external) continue
    anchor.setAttribute('target', '_blank')
    anchor.setAttribute('rel', 'noopener noreferrer')
  }
}

/** Render untrusted Markdown to sanitized HTML. */
export function renderMarkdown(text: string, origin: string = window.location.origin): string {
  if (!text) return ''
  let html: string
  let stash: string[] = []
  try {
    const stashed = stashMath(inlineQrImages(text))
    stash = stashed.stash
    html = String(marked.parse(stashed.text, { async: false }))
  } catch {
    html = `<p>${escapeHtml(text).replace(/\n/g, '<br>')}</p>`
  }
  const template = document.createElement('template')
  template.innerHTML = String(DOMPurify.sanitize(restoreMath(html, stash), PURIFY))
  wrapTables(template.content)
  linkRemoteImages(template.content)
  routeLinks(template.content, origin)
  // The post-passes only add controlled markup; sanitize once more last.
  return String(DOMPurify.sanitize(template.innerHTML, PURIFY))
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
  throw new Error('Clipboard unavailable')
}

/** Sanitized Markdown with working code-block Copy buttons. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(text), [text])
  const onClick = useCallback((e: MouseEvent<HTMLDivElement>) => {
    const button = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-copy="code"]')
    if (!button) return
    const code = button.closest('.code-block')?.querySelector('pre')?.textContent ?? ''
    void copyText(code)
      .then(() => toast.success(t('webChat.copied')))
      .catch(() => toast.error(t('webChat.copyFailed')))
  }, [])
  return (
    <div
      className={cn('prose-chat', className)}
      onClick={onClick}
      // Sanitized by renderMarkdown (DOMPurify allowlist, twice).
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
