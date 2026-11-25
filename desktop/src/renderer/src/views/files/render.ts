import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import diff from 'highlight.js/lib/languages/diff'
import go from 'highlight.js/lib/languages/go'
import ini from 'highlight.js/lib/languages/ini'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdownLanguage from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import rust from 'highlight.js/lib/languages/rust'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
import 'highlight.js/styles/github-dark.css'
import { Marked } from 'marked'
import { HIGHLIGHT_MAX_CHARS } from './logic'

/**
 * Rendering for the file browser's previews. A workspace file is untrusted
 * input like any other: the agent wrote it, or fetched it from the web. So
 * nothing here ever becomes live markup. Markdown goes through the sanitizer
 * and then loses everything that could load, navigate or run; an HTML or SVG
 * file is only ever shown as its source.
 */

// The lightweight core plus the grammars a workspace actually holds. The chat
// renderer registers some of these on the same core; registering twice is a
// no-op, and this module does not depend on the chat having loaded.
const LANGUAGES = {
  bash,
  css,
  diff,
  go,
  ini,
  javascript,
  json,
  markdown: markdownLanguage,
  python,
  rust,
  sql,
  typescript,
  xml,
  yaml,
} as const
for (const [name, grammar] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, grammar)

const LANGUAGE_ALIASES: Record<string, string> = {
  sh: 'bash',
  shell: 'bash',
  zsh: 'bash',
  js: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  yml: 'yaml',
  html: 'xml',
  toml: 'ini',
  md: 'markdown',
  rs: 'rust',
  golang: 'go',
  patch: 'diff',
}

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function resolveLanguage(language: string | undefined): string | null {
  const key = (language || '').toLowerCase()
  const name = LANGUAGE_ALIASES[key] ?? key
  return name && hljs.getLanguage(name) ? name : null
}

/**
 * Source as highlighted HTML: escaped text wrapped in highlight.js's own
 * `<span class="hljs-…">`. An unknown language, or a file past the
 * highlighting budget, comes back as plain escaped text.
 */
export function highlightCode(code: string, language: string | undefined): string {
  const name = resolveLanguage(language)
  if (!name || code.length > HIGHLIGHT_MAX_CHARS) return escapeHtml(code)
  try {
    return hljs.highlight(code, { language: name, ignoreIllegals: true }).value
  } catch {
    return escapeHtml(code)
  }
}

const markdown = new Marked({
  gfm: true,
  breaks: false,
  renderer: {
    // A task list's checkbox as a glyph: form controls are not let through.
    checkbox({ checked }) {
      return checked ? '\u2611 ' : '\u2610 '
    },
  },
})

/** Tags a preview has no use for; the sanitizer drops them and their content. */
const FORBIDDEN_TAGS = [
  'style',
  'link',
  'meta',
  'base',
  'form',
  'input',
  'button',
  'textarea',
  'select',
  'option',
  'iframe',
  'frame',
  'object',
  'embed',
  'video',
  'audio',
  'source',
  'track',
  'picture',
  'svg',
  'math',
  'dialog',
  'details',
  'summary',
]
const FORBIDDEN_ATTRS = ['style', 'id', 'name', 'srcset', 'background', 'poster', 'ping']
const EXTERNAL_LINK = /^(https?:|mailto:)/i

export interface MarkdownLabels {
  /** Shown in place of a remote image, before a link to it. */
  remoteImage: string
  /** Shown in place of an image stored beside the file. */
  localImage: string
}

function tameImages(root: DocumentFragment, labels: MarkdownLabels): void {
  for (const image of root.querySelectorAll('img')) {
    const src = image.getAttribute('src') || ''
    // An image already in hand. SVG is excluded even inline: only raster data.
    if (/^data:image\/(png|jpeg|gif|webp);base64,/i.test(src)) {
      image.removeAttribute('srcset')
      continue
    }
    const note = document.createElement('span')
    note.className = 'files-md__blocked'
    const alt = image.getAttribute('alt') || ''
    if (/^https?:/i.test(src)) {
      // Loading it would tell its host that this file was opened, and when.
      let host = src
      try {
        host = new URL(src).host
      } catch {
        /* keep the raw src */
      }
      note.append(`${labels.remoteImage} `)
      const link = document.createElement('a')
      link.setAttribute('href', src)
      link.textContent = alt || host
      note.appendChild(link)
    } else {
      note.textContent = alt ? `${labels.localImage} ${alt}` : labels.localImage
    }
    image.replaceWith(note)
  }
}

function tameLinks(root: DocumentFragment): void {
  for (const anchor of root.querySelectorAll('a')) {
    const href = anchor.getAttribute('href') || ''
    for (const attr of [...anchor.attributes]) {
      if (attr.name !== 'href' && attr.name !== 'title') anchor.removeAttribute(attr.name)
    }
    if (!EXTERNAL_LINK.test(href)) {
      // A relative link or an in-page anchor has nowhere to go from a
      // preview: it stays as text rather than navigating the app's window.
      anchor.removeAttribute('href')
      continue
    }
    // Opened by the main process in the default browser, never in the app.
    anchor.setAttribute('target', '_blank')
    anchor.setAttribute('rel', 'noreferrer noopener')
  }
}

function highlightBlocks(root: DocumentFragment): void {
  for (const code of root.querySelectorAll('pre > code')) {
    const language = /(?:^|\s)language-([\w+#-]+)/.exec(code.getAttribute('class') || '')?.[1]
    const text = code.textContent || ''
    code.innerHTML = highlightCode(text, language)
    code.setAttribute('class', 'hljs')
  }
}

function stripClasses(root: DocumentFragment): void {
  for (const element of root.querySelectorAll('[class]')) {
    const tag = element.tagName
    const keep =
      (tag === 'CODE' && element.parentElement?.tagName === 'PRE') ||
      (tag === 'SPAN' && element.closest('pre') !== null) ||
      element.classList.contains('files-md__blocked')
    if (!keep) element.removeAttribute('class')
  }
}

/**
 * Markdown as HTML that is safe to set as `innerHTML`.
 *
 * Sanitized first, then tamed in the DOM (remote and relative images become
 * notes, only http(s)/mailto links stay links, classes the file brought are
 * dropped, fenced code is highlighted), then sanitized once more so the last
 * thing to touch the markup is the sanitizer.
 */
export function renderMarkdown(text: string, labels: MarkdownLabels): string {
  if (!text) return ''
  let parsed: string
  try {
    parsed = String(markdown.parse(text, { async: false }))
  } catch {
    return `<pre>${escapeHtml(text)}</pre>`
  }
  const options = {
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    FORBID_TAGS: FORBIDDEN_TAGS,
    FORBID_ATTR: FORBIDDEN_ATTRS,
  }
  const template = document.createElement('template')
  template.innerHTML = String(DOMPurify.sanitize(parsed, options))
  const root = template.content
  // Classes first: the passes below add the only ones that should remain.
  for (const element of root.querySelectorAll('[class]')) {
    const isFence = element.tagName === 'CODE' && element.parentElement?.tagName === 'PRE'
    if (!isFence) element.removeAttribute('class')
  }
  highlightBlocks(root)
  tameImages(root, labels)
  tameLinks(root)
  stripClasses(root)
  // Wide tables scroll inside their own box instead of widening the preview.
  for (const table of root.querySelectorAll('table')) {
    const wrap = document.createElement('div')
    wrap.className = 'files-md__table'
    table.replaceWith(wrap)
    wrap.appendChild(table)
  }
  return String(
    DOMPurify.sanitize(template.innerHTML, {
      ...options,
      ADD_ATTR: ['target'],
    }),
  )
}
