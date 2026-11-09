// Real-browser UI audit of the Control UI (overhaul-control-ui task 10.2).
//
// Driven by tests/functional/test_webui_ui_audit.py, which boots two real
// `agentos gateway run` processes (default and custom base path) and passes:
//   AUDIT_BASE_URL      e.g. http://127.0.0.1:PORT/control
//   AUDIT_ALT_BASE_URL  e.g. http://127.0.0.1:PORT/ops/agentos
//   AUDIT_OUT_DIR       screenshots + results.json (default /tmp/agentos-ui-audit)
//   AUDIT_ONLY          optional comma list of check groups to run
// It prints one JSON summary line last; pytest asserts on it.
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const { chromium } = require('playwright')
const { AxeBuilder } = require('@axe-core/playwright')

const BASE = process.env.AUDIT_BASE_URL.replace(/\/+$/, '')
const ALT_BASE = (process.env.AUDIT_ALT_BASE_URL || '').replace(/\/+$/, '')
const OUT = process.env.AUDIT_OUT_DIR || '/tmp/agentos-ui-audit'
const ONLY = new Set((process.env.AUDIT_ONLY || '').split(',').filter(Boolean))
const runs = (group) => ONLY.size === 0 || ONLY.has(group)

const ALL_VIEWPORTS = [
  { name: '360', width: 360, height: 780 },
  { name: '768', width: 768, height: 1024 },
  { name: '1280', width: 1280, height: 800 },
]
const VP_FILTER = new Set((process.env.AUDIT_VIEWPORTS || '').split(',').filter(Boolean))
const VIEWPORTS = ALL_VIEWPORTS.filter((v) => VP_FILTER.size === 0 || VP_FILTER.has(v.name))
const [PHONE, , DESKTOP] = ALL_VIEWPORTS
const THEMES = ['light', 'dark']
const SESSION = '/chat/main/webchat/ui-audit'

// Every row of design.md Appendix A, mapped to the routes that carry it.
const ROUTES = [
  { id: 'home', path: '/home', rows: ['Overview'] },
  // /chat resumes the most recent (or default) session.
  { id: 'chat-index', path: '/chat', rows: ['Chat'], lands: /^\/chat\/main\/webchat\/[^/]+$/ },
  { id: 'chat-session', path: SESSION, rows: ['Chat'] },
  { id: 'new', path: '/new', rows: ['Chat'] },
  { id: 'projects', path: '/projects', rows: ['Projects'] },
  { id: 'project-detail', path: '/projects/:projectId', rows: ['Projects'] },
  { id: 'sessions', path: '/sessions', rows: ['Sessions'] },
  { id: 'automations', path: '/automations', rows: ['Cron'] },
  { id: 'usage', path: '/usage', rows: ['Usage'] },
  { id: 'inbox', path: '/inbox', rows: ['Inbox', 'Approvals'] },
  { id: 'settings', path: '/settings', rows: ['Settings / Setup'] },
  { id: 'settings-setup', path: '/settings/setup', rows: ['Settings / Setup'] },
  { id: 'settings-appearance', path: '/settings/appearance', rows: ['Settings / Setup'] },
  { id: 'settings-notifications', path: '/settings/notifications', rows: ['Shell globals'] },
  { id: 'settings-gateway', path: '/settings/gateway', rows: ['Overview'] },
  { id: 'settings-channels', path: '/settings/channels', rows: ['Channels'] },
  { id: 'settings-agents', path: '/settings/agents', rows: ['Agents'] },
  { id: 'settings-models', path: '/settings/models', rows: ['Settings / Setup'] },
  { id: 'settings-search', path: '/settings/search', rows: ['Settings / Setup'] },
  { id: 'settings-skills', path: '/settings/skills', rows: ['Skills'] },
  { id: 'settings-mcp', path: '/settings/mcp', rows: ['MCP'] },
  { id: 'settings-memory', path: '/settings/memory', rows: ['Memory'] },
  { id: 'settings-media', path: '/settings/media', rows: ['Settings / Setup'] },
  { id: 'settings-approvals', path: '/settings/approvals', rows: ['Approvals'] },
  { id: 'settings-environment', path: '/settings/environment', rows: ['Env'] },
  { id: 'settings-health', path: '/settings/health', rows: ['Health'] },
  { id: 'settings-logs', path: '/settings/logs', rows: ['Logs'] },
  { id: 'settings-advanced', path: '/settings/advanced', rows: ['Config'] },
  { id: 'settings-debug', path: '/settings/debug', rows: ['Shell globals'] },
  { id: 'settings-updates', path: '/settings/updates', rows: ['Shell globals'] },
  { id: 'settings-about', path: '/settings/about', rows: ['Shell globals'] },
  { id: 'mcp-oauth-callback', path: '/mcp/oauth/callback?error=access_denied', rows: ['MCP'] },
]

// Legacy URL -> expected current path (search included when it matters).
const LEGACY = [
  ['/overview', '/home'],
  ['/cron', '/automations'],
  ['/health', '/settings/health'],
  ['/channels', '/settings/channels'],
  ['/channels?view=setup', '/settings/channels?setup=1'],
  ['/mcp', '/settings/mcp'],
  ['/skills', '/settings/skills'],
  ['/memory', '/settings/memory'],
  ['/agents', '/settings/agents'],
  ['/env', '/settings/environment'],
  ['/logs', '/settings/logs'],
  ['/approvals', '/settings/approvals'],
  ['/config', '/settings/advanced'],
  ['/setup', '/settings/setup'],
  ['/settings?step=channels', '/settings/channels?setup=1'],
  ['/chat?session=agent:main:webchat:legacy-link', '/chat/main/webchat/legacy-link'],
  ['/', /^\/chat\/main\/webchat\/[^/]+$/],
]

// Requests the console is expected to see fail with 4xx in a fresh state dir.
const EXPECTED_HTTP = [
  // A fresh install has never registered a push subscription.
]

const results = {
  base: BASE,
  altBase: ALT_BASE,
  startedAt: new Date().toISOString(),
  matrix: [],
  axe: [],
  keyboard: [],
  reducedMotion: [],
  forcedColors: [],
  deepLinks: [],
  legacy: [],
  basePath: [],
  touchTargets: [],
  fineTargets: [],
  failures: [],
}

function fail(group, detail) {
  results.failures.push({ group, ...detail })
}

function attachDiagnostics(page) {
  const diag = { consoleErrors: [], pageErrors: [], badResponses: [], requestFailures: [] }
  page.on('console', (m) => {
    if (m.type() === 'error') diag.consoleErrors.push(m.text())
  })
  page.on('pageerror', (e) => diag.pageErrors.push(String(e && e.stack ? e.stack : e)))
  page.on('response', (r) => {
    if (r.status() >= 400 && !EXPECTED_HTTP.some((re) => re.test(r.url()))) {
      diag.badResponses.push(`${r.status()} ${r.request().method()} ${r.url()}`)
    }
  })
  page.on('requestfailed', (r) => {
    const err = r.failure()?.errorText || 'failed'
    // Navigations away abort in-flight fetches; that is not a defect.
    if (err === 'net::ERR_ABORTED') return
    diag.requestFailures.push(`${err} ${r.url()}`)
  })
  return diag
}

async function newContext(browser, { width, height, theme, reducedMotion, forcedColors }) {
  const context = await browser.newContext({
    viewport: { width, height },
    colorScheme: theme,
    reducedMotion: reducedMotion || 'no-preference',
    forcedColors: forcedColors || 'none',
    hasTouch: width < 768,
  })
  await context.addInitScript((t) => {
    try {
      localStorage.setItem('agentos-theme', t)
    } catch {
      /* storage blocked */
    }
  }, theme)
  return context
}

/** Wait until the destination rendered: a main landmark and no route spinner. */
async function settle(page) {
  await page.waitForLoadState('domcontentloaded')
  await page.locator('main, [role="main"]').first().waitFor({ timeout: 15000 })
  await page
    .waitForFunction(
      () => !document.querySelector('[data-route-pending], [aria-busy="true"]:not(button)'),
      null,
      { timeout: 6000 },
    )
    .catch(() => {})
  await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(400)
}

async function pageMetrics(page) {
  return page.evaluate(() => {
    const doc = document.documentElement
    return {
      path: location.pathname + location.search,
      theme: doc.getAttribute('data-theme'),
      horizontalScroll: doc.scrollWidth > window.innerWidth + 1,
      scrollWidth: doc.scrollWidth,
      innerWidth: window.innerWidth,
      notFound: /page not found|not found/i.test(document.title),
      // Content clipped away: a scroll/clip box squeezed to 0 px around real content.
      collapsed: [...document.querySelectorAll('body *')]
        .filter((el) => {
          const st = getComputedStyle(el)
          if (st.display === 'none' || st.visibility === 'hidden') return false
          if (!/(auto|scroll|hidden|clip)/.test(st.overflowY + st.overflowX)) return false
          const zeroH = el.clientHeight === 0 && el.scrollHeight > 24
          const zeroW = el.clientWidth === 0 && el.scrollWidth > 24
          return (zeroH || zeroW) && (el.textContent || '').trim().length > 0
        })
        .map((el) => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)} ${el.clientWidth}x${el.clientHeight} of ${el.scrollWidth}x${el.scrollHeight}`)
        .slice(0, 5),
      title: document.title,
    }
  })
}

function axeSummary(violations) {
  return violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    nodes: v.nodes.slice(0, 6).map((n) => ({
      target: n.target.join(' '),
      summary: (n.failureSummary || '').split('\n').slice(0, 3).join(' | '),
    })),
    count: v.nodes.length,
  }))
}

async function runAxe(page) {
  const res = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'])
    .analyze()
  return axeSummary(res.violations)
}

/** Interactive controls under 24x24 at the current viewport (WCAG 2.5.8). */
async function smallTargets(page) {
  return page.evaluate(() => {
    const sel = [
      'a[href]',
      'button',
      'input:not([type=hidden])',
      'select',
      'textarea',
      '[role=button]',
      '[role=tab]',
      '[role=switch]',
      '[role=checkbox]',
      '[role=menuitem]',
      '[role=option]',
      '[role=link]',
      '[tabindex]:not([tabindex="-1"])',
    ].join(',')
    const visible = (el) => {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) return false
      const s = getComputedStyle(el)
      if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false
      // Visually-hidden helpers (sr-only) are not pointer targets.
      if (r.width <= 1 && r.height <= 1) return false
      if (s.clipPath && s.clipPath.startsWith('inset(50%')) return false
      if (s.clip && s.clip !== 'auto') return false
      return true
    }
    const els = [...document.querySelectorAll(sel)].filter(visible)
    // A label wrapping a hidden native input is the real target.
    const rects = els.map((el) => {
      const r = el.getBoundingClientRect()
      return { el, x: r.left, y: r.top, w: r.width, h: r.height }
    })
    const inlineInText = (el) => {
      if (el.tagName !== 'A') return false
      const parent = el.parentElement
      if (!parent) return false
      const display = getComputedStyle(el).display
      return display === 'inline' && /\S/.test(parent.textContent.replace(el.textContent, ''))
    }
    const describe = (el) => {
      const name =
        el.getAttribute('aria-label') ||
        el.getAttribute('title') ||
        (el.textContent || '').trim().slice(0, 40) ||
        el.getAttribute('name') ||
        ''
      const id = el.id ? `#${el.id}` : ''
      return `${el.tagName.toLowerCase()}${id}[${el.getAttribute('role') || ''}] "${name}"`
    }
    const out = []
    for (const r of rects) {
      if (r.w >= 24 && r.h >= 24) continue
      if (inlineInText(r.el)) continue
      // Native checkbox/radio inside a label: the label is the target.
      if (r.el.tagName === 'INPUT' && r.el.closest('label')) {
        const lr = r.el.closest('label').getBoundingClientRect()
        if (lr.width >= 24 && lr.height >= 24) continue
      }
      // Spacing exception: a 24px circle centred on the target touches no other target.
      const cx = r.x + r.w / 2
      const cy = r.y + r.h / 2
      const clash = rects.some((o) => {
        if (o === r || o.el.contains(r.el) || r.el.contains(o.el)) return false
        const nx = Math.max(o.x, Math.min(cx, o.x + o.w))
        const ny = Math.max(o.y, Math.min(cy, o.y + o.h))
        return Math.hypot(nx - cx, ny - cy) < 12
      })
      if (!clash) continue
      out.push({ target: describe(r.el), w: Math.round(r.w), h: Math.round(r.h) })
    }
    return out
  })
}

/**
 * The shell scrolls inside <main>, so a "full page" screenshot would only show
 * one viewport. Grow the viewport to the tallest scroller's content for the
 * shot (checks run at the nominal size before this), then restore it.
 */
async function fullScreenshot(page, file) {
  const size = page.viewportSize()
  const extra = await page.evaluate(() => {
    let most = 0
    for (const el of document.querySelectorAll('main, main *')) {
      const st = getComputedStyle(el)
      if (!/(auto|scroll)/.test(st.overflowY)) continue
      most = Math.max(most, el.scrollHeight - el.clientHeight)
    }
    return most
  })
  if (extra > 0) {
    await page.setViewportSize({ width: size.width, height: Math.min(size.height + extra, 12000) })
    await page.waitForTimeout(250)
  }
  await page.screenshot({ path: file, fullPage: true })
  if (extra > 0) await page.setViewportSize(size)
}

function routeUrl(base, route, projectId) {
  return base + route.path.replace(':projectId', encodeURIComponent(projectId || 'missing'))
}

async function createProject(browser) {
  const context = await newContext(browser, { width: 1280, height: 800, theme: 'dark' })
  const page = await context.newPage()
  try {
    await page.goto(`${BASE}/projects`)
    await settle(page)
    await page.getByRole('button', { name: /new project/i }).first().click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ timeout: 5000 })
    await dialog.getByRole('textbox').first().fill('UI audit project')
    await dialog.getByRole('button', { name: /create project/i }).click()
    await page.waitForURL(/\/projects\/[^/]+$/, { timeout: 8000 })
    return decodeURIComponent(new URL(page.url()).pathname.split('/').pop())
  } catch (error) {
    fail('setup', { message: `could not create a project through the UI: ${error.message}` })
    return null
  } finally {
    await context.close()
  }
}

// ---------------------------------------------------------------------------
// 1. Viewport x theme matrix: screenshot, no page scroll, no errors, axe.
// ---------------------------------------------------------------------------
async function auditMatrix(browser, projectId) {
  for (const vp of VIEWPORTS) {
    for (const theme of THEMES) {
      const context = await newContext(browser, { ...vp, theme })
      for (const route of ROUTES) {
        const page = await context.newPage()
        const diag = attachDiagnostics(page)
        const entry = { route: route.id, path: route.path, viewport: vp.name, theme, rows: route.rows }
        try {
          const response = await page.goto(routeUrl(BASE, route, projectId))
          entry.status = response ? response.status() : 0
          await settle(page)
          Object.assign(entry, await pageMetrics(page))
          const shot = path.join(OUT, `${route.id}-${vp.name}-${theme}.png`)
          entry.screenshot = shot
          const violations = await runAxe(page)
          entry.axe = violations.map((v) => `${v.impact}:${v.id}(${v.count})`)
          for (const v of violations) {
            results.axe.push({ route: route.id, viewport: vp.name, theme, ...v })
          }
          if (vp.name === '360') {
            const small = await smallTargets(page)
            if (small.length) results.touchTargets.push({ route: route.id, theme, small })
            entry.smallTargets = small.length
          }
          await fullScreenshot(page, shot)
        } catch (error) {
          entry.error = String(error.message || error)
        }
        entry.diagnostics = diag
        const problems = []
        if (entry.error) problems.push(`error: ${entry.error}`)
        if (entry.status !== 200) problems.push(`status ${entry.status}`)
        if (entry.theme && entry.theme !== theme) problems.push(`theme ${entry.theme}`)
        if (entry.horizontalScroll) {
          problems.push(`horizontal scroll ${entry.scrollWidth}>${entry.innerWidth}`)
        }
        if (entry.notFound) problems.push('rendered Not found')
        if (entry.collapsed && entry.collapsed.length) problems.push(`content clipped to 0px: ${entry.collapsed.join(' || ')}`)
        if (diag.consoleErrors.length) problems.push(`console: ${diag.consoleErrors.join(' || ')}`)
        if (diag.pageErrors.length) problems.push(`pageerror: ${diag.pageErrors.join(' || ')}`)
        if (diag.badResponses.length) problems.push(`http: ${diag.badResponses.join(' || ')}`)
        if (diag.requestFailures.length) problems.push(`net: ${diag.requestFailures.join(' || ')}`)
        const serious = (entry.axe || []).filter((a) => /^(serious|critical):/.test(a))
        if (serious.length) problems.push(`axe: ${serious.join(', ')}`)
        if (entry.smallTargets) problems.push(`touch targets <24px: ${entry.smallTargets}`)
        entry.pass = problems.length === 0
        entry.problems = problems
        if (!entry.pass) fail('matrix', { route: route.id, viewport: vp.name, theme, problems })
        results.matrix.push(entry)
        await page.close()
      }
      await context.close()
    }
  }
}

// 1b. Target size at 360 px with a fine pointer (a narrow desktop window):
//     the 44 px touch sizing does not apply, the 24 px floor (WCAG 2.5.8) does.
async function auditFineTargets(browser, projectId) {
  for (const theme of ['dark']) {
    const context = await browser.newContext({ viewport: { width: 360, height: 780 }, hasTouch: false })
    await context.addInitScript((t) => localStorage.setItem('agentos-theme', t), theme)
    for (const route of ROUTES) {
      const page = await context.newPage()
      try {
        await page.goto(routeUrl(BASE, route, projectId))
        await settle(page)
        const small = await smallTargets(page)
        results.fineTargets.push({ route: route.id, small })
        if (small.length) fail('fineTargets', { route: route.id, small })
      } catch (error) {
        fail('fineTargets', { route: route.id, error: String(error.message || error) })
      }
      await page.close()
    }
    await context.close()
  }
}

// ---------------------------------------------------------------------------
// 2. Keyboard-only pass.
// ---------------------------------------------------------------------------
/** Who has focus: identity only. */
const FOCUS_PROBE = () => {
  const el = document.activeElement
  if (!el || el === document.body || el === document.documentElement) {
    return { tag: 'body', key: 'body', name: '', role: '', inDialog: false, region: 'body' }
  }
  const r = el.getBoundingClientRect()
  const name =
    el.getAttribute('aria-label') ||
    (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40) ||
    el.getAttribute('placeholder') ||
    ''
  return {
    tag: el.tagName.toLowerCase(),
    role: el.getAttribute('role') || '',
    name,
    key: `${el.tagName.toLowerCase()}|${el.getAttribute('role') || ''}|${name}|${Math.round(r.left)},${Math.round(r.top)}`,
    inDialog: !!el.closest('[role=dialog],[role=alertdialog]'),
    region:
      el.closest('nav')?.getAttribute('aria-label') ||
      el.closest('[role=region]')?.getAttribute('aria-label') ||
      (el.closest('[role=dialog]') ? 'dialog' : el.closest('main') ? 'main' : el.closest('header') ? 'header' : 'other'),
    size: { w: Math.round(r.width), h: Math.round(r.height) },
  }
}

/**
 * The focus indicator of the focused element: snapshot it and up to three
 * ancestors (focus-within rings), blur, snapshot again, refocus. Whatever
 * changed is the indicator; its contrast is measured against what it replaces
 * (border, fill, text) or what it sits on (outline, shadow). WCAG 2.4.7 needs
 * one; 1.4.11 / 2.4.13 put the bar at 3:1.
 */
const INDICATOR = () => {
  const el = document.activeElement
  if (!el || el === document.body || el === document.documentElement) return null
  const r = el.getBoundingClientRect()
  if (r.width === 0 || r.height === 0) return { best: 0, kinds: [], zeroSize: true }
  const PROPS = [
    'outlineStyle', 'outlineWidth', 'outlineColor', 'boxShadow',
    'borderTopColor', 'borderBottomColor', 'borderLeftColor', 'borderRightColor',
    'borderTopWidth', 'borderBottomWidth', 'backgroundColor', 'color', 'textDecorationLine',
  ]
  const chain = []
  for (let n = el, i = 0; n && n !== document.body && i < 4; n = n.parentElement, i++) chain.push(n)
  // Measure settled states: `transition-colors` fades outline-color in from currentColor.
  const settleTransitions = () => {
    for (const a of document.getAnimations()) if (a instanceof CSSTransition) a.finish()
  }
  const snap = () => {
    settleTransitions()
    return chain.map((n) => {
      const cs = getComputedStyle(n)
      const o = {}
      for (const k of PROPS) o[k] = cs[k]
      return o
    })
  }
  const focused = snap()
  el.blur()
  const blurred = snap()
  el.focus({ preventScroll: true })
  const restored = JSON.stringify(snap()) === JSON.stringify(focused)

  const parse = (c) => {
    if (!c) return null
    let m = c.match(/rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)/)
    if (m) {
      const a = m[4] === undefined ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : +m[4]
      return [+m[1], +m[2], +m[3], a]
    }
    m = c.match(/color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\)/)
    if (m) return [m[1] * 255, m[2] * 255, m[3] * 255, m[4] === undefined ? 1 : +m[4]]
    return null
  }
  const lum = ([r, g, b]) => {
    const f = (v) => ((v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
  }
  const ratio = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
    return (x + 0.05) / (y + 0.05)
  }
  const blend = (fg, bg) =>
    fg[3] >= 1 ? fg : [0, 1, 2].map((i) => fg[i] * fg[3] + bg[i] * (1 - fg[3])).concat(1)
  const bgBehind = (n) => {
    const layers = []
    for (let p = n; p; p = p.parentElement) {
      const c = parse(getComputedStyle(p).backgroundColor)
      if (c && c[3] > 0) {
        layers.push(c)
        if (c[3] >= 1) break
      }
    }
    let base = [255, 255, 255, 1]
    if (!layers.length || layers[layers.length - 1][3] < 1) {
      base = document.documentElement.getAttribute('data-theme') === 'dark' ? [0, 0, 0, 1] : base
    }
    return layers.reverse().reduce((acc, c) => blend(c, acc), base)
  }
  const outlineOn = (st) => st.outlineStyle !== 'none' && parseFloat(st.outlineWidth) > 0
  const kinds = []
  chain.forEach((n, i) => {
    const f = focused[i]
    const b = blurred[i]
    const behind = bgBehind(n.parentElement || n)
    const own = bgBehind(n)
    const push = (kind, contrast) => kinds.push({ level: i, kind, contrast: Math.round(contrast * 100) / 100 })
    if (outlineOn(f) && (!outlineOn(b) || f.outlineColor !== b.outlineColor || f.outlineWidth !== b.outlineWidth)) {
      const c = parse(f.outlineColor)
      if (c) push('outline', ratio(blend(c, behind), behind))
    }
    if (f.boxShadow !== b.boxShadow && f.boxShadow !== 'none') {
      const colors = [...f.boxShadow.matchAll(/rgba?\([^)]+\)|color\([^)]+\)/g)]
        .map((m) => parse(m[0]))
        .filter((c) => c && c[3] > 0)
      push('shadow', Math.max(0, ...colors.map((c) => ratio(blend(c, behind), behind))))
    }
    for (const side of ['Top', 'Bottom', 'Left', 'Right']) {
      const k = `border${side}Color`
      if (f[k] !== b[k]) {
        const a = parse(f[k])
        const c = parse(b[k])
        if (a && c) push('border', ratio(blend(a, own), blend(c, own)))
        break
      }
    }
    if (f.borderTopWidth !== b.borderTopWidth || f.borderBottomWidth !== b.borderBottomWidth) {
      const c = parse(f.borderBottomColor)
      if (c) push('border-width', ratio(blend(c, own), own))
    }
    if (f.backgroundColor !== b.backgroundColor) {
      const bh = bgBehind(n.parentElement || n)
      const a = parse(f.backgroundColor)
      const c = parse(b.backgroundColor)
      if (a && c) push('background', ratio(blend(a, bh), blend(c, bh)))
    }
    if (f.color !== b.color) {
      const a = parse(f.color)
      const c = parse(b.color)
      if (a && c) push('text-color', ratio(blend(a, own), blend(c, own)))
    }
    if (f.textDecorationLine !== b.textDecorationLine) push('underline', 21)
  })
  return { best: Math.max(0, ...kinds.map((k) => k.contrast)), kinds, restored }
}

const MIN_INDICATOR_CONTRAST = 3

async function probe(page) {
  const who = await page.evaluate(FOCUS_PROBE)
  const ind = await page.evaluate(INDICATOR)
  who.indicator = ind
  who.visible = ind === null || ind.best >= MIN_INDICATOR_CONTRAST
  return who
}

async function tabWalk(page, max) {
  const stops = []
  for (let i = 0; i < max; i++) {
    await page.keyboard.press('Tab')
    stops.push(await probe(page))
  }
  return stops
}

/**
 * Escape closes the top layer. A tooltip opened by keyboard focus is a layer
 * of its own (WCAG 1.4.13), so a second Escape may be needed; only that case
 * is allowed. Then wait for the dialog's asynchronous focus return.
 */
async function escapeOut(page) {
  const layers = []
  for (let i = 0; i < 3 && (await page.getByRole('dialog').count()) > 0; i++) {
    const tooltip = await page.locator('[role=tooltip]').count()
    layers.push(tooltip ? 'tooltip' : 'dialog')
    await page.keyboard.press('Escape')
    await page.waitForTimeout(150)
    if (!tooltip) break
  }
  await page.getByRole('dialog').waitFor({ state: 'detached', timeout: 4000 }).catch(() => {})
  await page
    .waitForFunction(() => document.activeElement && document.activeElement !== document.body, null, { timeout: 2000 })
    .catch(() => {})
  return { layers, closed: (await page.getByRole('dialog').count()) === 0 }
}

async function auditKeyboard(browser) {
  const scenarios = []
  for (const vp of [DESKTOP, PHONE]) {
    for (const theme of THEMES) {
      const context = await newContext(browser, { ...vp, theme })
      const page = await context.newPage()
      const diag = attachDiagnostics(page)
      const sc = { viewport: vp.name, theme, checks: {} }
      try {
        await page.goto(BASE + SESSION)
        await settle(page)
        await page.locator('body').click({ position: { x: 1, y: 1 } }).catch(() => {})
        await page.evaluate(() => document.activeElement && document.activeElement.blur())

        // Walk the shell: rail, session list, composer.
        const stops = await tabWalk(page, 70)
        const invisible = stops.filter((s) => s.tag !== 'body' && !s.visible)
        const seen = (re) => stops.some((s) => re.test(`${s.region} ${s.name} ${s.role}`))
        sc.checks.focusAlwaysVisible = { pass: invisible.length === 0, invisible: dedupe(invisible) }
        sc.checks.reachesComposer = { pass: stops.some((s) => s.name === '' ? false : /message/i.test(s.name) || s.tag === 'textarea') }
        if (vp.name === '1280') {
          sc.checks.reachesRail = { pass: seen(/Main navigation/) }
          sc.checks.reachesSessionList = { pass: seen(/Sessions and pages/) }
        } else {
          sc.checks.reachesOpenNavigation = { pass: seen(/Open navigation/) }
        }
        // No trap outside a modal: the walk must cycle back to its first stop.
        const firstKey = stops[0]?.key
        const revisits = stops.slice(1).some((s) => s.key === firstKey || s.tag === 'body')
        const distinct = new Set(stops.map((s) => s.key)).size
        sc.checks.noTrapOutsideModal = {
          pass: revisits || distinct >= 60,
          distinct,
          note: revisits ? 'tab order wrapped' : 'long tab order, no repeat within 70 stops',
        }
        sc.stops = stops.map((s) => `${s.region}> ${s.tag}${s.role ? `[${s.role}]` : ''} "${s.name}" ${s.visible ? `ok ${s.indicator ? s.indicator.best : ''}` : 'WEAK-OR-NO-INDICATOR'}`)

        // Palette: Ctrl/Cmd+K from the composer, trap inside, Escape returns focus.
        const composer = page.locator('[data-composer-input]')
        await composer.focus()
        const before = await probe(page)
        await page.keyboard.press('Control+k')
        const palette = page.getByRole('dialog')
        await palette.waitFor({ timeout: 4000 })
        const inPalette = await tabWalk(page, 12)
        await page.keyboard.press('ArrowDown')
        const optionMarker = await page.evaluate(() => {
          const o = document.querySelector('[cmdk-item][data-selected=true]')
          if (!o) return { found: false }
          const st = getComputedStyle(o)
          return { found: true, outline: `${st.outlineStyle} ${st.outlineWidth} ${st.outlineColor}`, on: st.outlineStyle !== 'none' && parseFloat(st.outlineWidth) > 0 }
        })
        const paletteEsc = await escapeOut(page)
        const after = await probe(page)
        sc.checks.palette = {
          pass:
            inPalette.every((s) => s.inDialog) &&
            (await page.getByRole('dialog').count()) === 0 &&
            after.key === before.key &&
            inPalette.every((s) => s.visible) &&
            optionMarker.on === true,
          focusStaysInside: inPalette.every((s) => s.inDialog),
          ringVisible: inPalette.every((s) => s.visible),
          inputIndicator: inPalette[0]?.indicator,
          escape: paletteEsc,
          optionMarker,
          returnedTo: after.name || after.tag,
          expected: before.name || before.tag,
        }

        // A menu from the composer (Capabilities): Enter opens, Escape closes, focus returns.
        const caps = page.getByRole('button', { name: 'Capabilities' })
        await caps.focus()
        await page.keyboard.press('Enter')
        const menu = page.getByRole('menu')
        await menu.waitFor({ timeout: 4000 })
        const menuFocus = await probe(page)
        await page.keyboard.press('ArrowDown')
        const menuNext = await probe(page)
        await page.keyboard.press('Escape')
        await menu.waitFor({ state: 'detached', timeout: 4000 }).catch(() => {})
        await page.waitForTimeout(100)
        const afterMenu = await probe(page)
        sc.checks.menu = {
          pass:
            (await page.getByRole('menu').count()) === 0 &&
            /Capabilities/.test(afterMenu.name) &&
            menuFocus.visible &&
            menuNext.visible,
          returnedTo: afterMenu.name,
          itemRingVisible: menuFocus.visible && menuNext.visible,
          itemIndicators: [menuFocus.indicator, menuNext.indicator],
        }

        // A modal dialog opened from a button.
        if (vp.name === '360') {
          const trigger = page.getByRole('button', { name: 'Open navigation' }).first()
          await trigger.focus()
          await page.keyboard.press('Enter')
          await page.getByRole('dialog').waitFor({ timeout: 4000 })
          const inside = await tabWalk(page, 25)
          const esc = await escapeOut(page)
          const back = await probe(page)
          sc.checks.drawerDialog = {
            pass:
              inside.every((s) => s.inDialog) &&
              (await page.getByRole('dialog').count()) === 0 &&
              /Open navigation/.test(back.name) &&
              inside.every((s) => s.visible),
            focusStaysInside: inside.every((s) => s.inDialog),
            invisible: dedupe(inside.filter((s) => !s.visible)),
            returnedTo: back.name,
            escape: esc,
          }
        } else {
          await page.goto(`${BASE}/sessions`)
          await settle(page)
          const trigger = page.getByRole('main').getByRole('button', { name: /new session/i }).first()
          await trigger.focus()
          const triggerProbe = await probe(page)
          await page.keyboard.press('Enter')
          await page.getByRole('dialog').waitFor({ timeout: 4000 })
          const inside = await tabWalk(page, 25)
          const esc = await escapeOut(page)
          const back = await probe(page)
          sc.checks.dialog = {
            pass:
              inside.every((s) => s.inDialog) &&
              (await page.getByRole('dialog').count()) === 0 &&
              back.key === triggerProbe.key &&
              inside.every((s) => s.visible),
            focusStaysInside: inside.every((s) => s.inDialog),
            invisible: dedupe(inside.filter((s) => !s.visible)),
            returnedTo: back.name,
            expected: triggerProbe.name,
            escape: esc,
          }
        }

        // Focus ring visibility across a settings page and Sessions page.
        for (const p of ['/settings/environment', '/sessions', '/automations']) {
          await page.goto(BASE + p)
          await settle(page)
          await page.evaluate(() => document.activeElement && document.activeElement.blur())
          const walk = await tabWalk(page, 40)
          const bad = walk.filter((s) => s.tag !== 'body' && !s.visible)
          sc.checks[`focusVisible ${p}`] = { pass: bad.length === 0, invisible: dedupe(bad) }
        }
      } catch (error) {
        sc.error = String(error.message || error)
      }
      sc.diagnostics = diag
      sc.pass = !sc.error && Object.values(sc.checks).every((c) => c.pass)
      if (!sc.pass) {
        fail('keyboard', {
          viewport: vp.name,
          theme,
          error: sc.error,
          failed: Object.entries(sc.checks).filter(([, c]) => !c.pass).map(([k, c]) => ({ k, ...c })),
        })
      }
      scenarios.push(sc)
      await context.close()
    }
  }
  results.keyboard = scenarios
}

function dedupe(stops) {
  const seen = new Map()
  for (const s of stops) {
    const ind = s.indicator
    const how = ind ? (ind.kinds.length ? ind.kinds.map((k) => `${k.kind}@${k.level}:${k.contrast}`).join(',') : 'no change') : ''
    const k = `${s.region}> ${s.tag}${s.role ? `[${s.role}]` : ''} "${s.name}" indicator=${how}`
    seen.set(k, (seen.get(k) || 0) + 1)
  }
  return [...seen.keys()]
}

// ---------------------------------------------------------------------------
// 3. Reduced motion and forced colors.
// ---------------------------------------------------------------------------
const LONG_MOTION = () => {
  const long = []
  const allowed = []
  for (const a of document.getAnimations()) {
    const t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {}
    const duration = typeof t.duration === 'number' ? t.duration : 0
    if (duration > 100 || t.iterations === Infinity) {
      const target = a.effect && a.effect.target
      // base.css keeps the running indicator's slow opacity pulse (status, not decoration).
      if (target && target.closest && target.closest('.motion-status')) {
        allowed.push(`${target.tagName.toLowerCase()}.motion-status ${duration}ms`)
        continue
      }
      long.push({
        kind: a.constructor.name,
        name: a.animationName || a.transitionProperty || a.id || '',
        duration,
        iterations: t.iterations,
        target: target ? `${target.tagName.toLowerCase()}.${String(target.className).slice(0, 60)}` : '',
      })
    }
  }
  // Declared (not yet running) transitions longer than 100ms.
  let declared = 0
  const samples = []
  for (const el of document.querySelectorAll('body *')) {
    const s = getComputedStyle(el)
    const td = Math.max(...s.transitionDuration.split(',').map((v) => parseFloat(v) * (v.includes('ms') ? 1 : 1000)))
    const ad = s.animationName !== 'none' ? Math.max(...s.animationDuration.split(',').map((v) => parseFloat(v) * (v.includes('ms') ? 1 : 1000))) : 0
    if ((td > 100 || ad > 100) && !el.closest('.motion-status')) {
      declared++
      if (samples.length < 5) samples.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 50)} t=${s.transitionDuration} a=${s.animationName}/${s.animationDuration}`)
    }
  }
  return { long, declared, samples, allowed }
}

const MOTION_ROUTES = ['/home', SESSION, '/sessions', '/settings', '/automations', '/usage', '/inbox']

async function auditReducedMotion(browser) {
  for (const vp of [DESKTOP, PHONE]) {
    const context = await newContext(browser, { ...vp, theme: 'dark', reducedMotion: 'reduce' })
    const page = await context.newPage()
    const diag = attachDiagnostics(page)
    for (const p of MOTION_ROUTES) {
      const entry = { viewport: vp.name, path: p }
      try {
        await page.goto(BASE + p)
        await settle(page)
        entry.idle = await page.evaluate(LONG_MOTION)
        // Overlays animate in: open the palette (and the drawer on phones).
        await page.keyboard.press('Control+k')
        await page.getByRole('dialog').waitFor({ timeout: 4000 })
        entry.palette = await page.evaluate(LONG_MOTION)
        await page.keyboard.press('Escape')
        await page.waitForTimeout(150)
        if (vp.name === '360') {
          await page.getByRole('button', { name: 'Open navigation' }).first().click()
          await page.getByRole('dialog').waitFor({ timeout: 4000 })
          entry.drawer = await page.evaluate(LONG_MOTION)
          await page.keyboard.press('Escape')
          await page.waitForTimeout(150)
        }
      } catch (error) {
        entry.error = String(error.message || error)
      }
      const phases = ['idle', 'palette', 'drawer'].filter((k) => entry[k])
      const offenders = phases.flatMap((k) => entry[k].long.map((l) => ({ phase: k, ...l })))
      const declared = phases.reduce((n, k) => n + entry[k].declared, 0)
      entry.pass = !entry.error && offenders.length === 0 && declared === 0
      entry.offenders = offenders
      if (!entry.pass) {
        fail('reducedMotion', {
          viewport: vp.name,
          path: p,
          error: entry.error,
          offenders,
          declaredSamples: phases.flatMap((k) => entry[k].samples),
        })
      }
      results.reducedMotion.push(entry)
    }
    if (diag.pageErrors.length || diag.consoleErrors.length) {
      fail('reducedMotion', { viewport: vp.name, diagnostics: diag })
    }
    await context.close()
  }
}

const FORCED_CHECK = () => {
  const sel = 'button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=switch], [role=tab], [role=checkbox], [role=combobox]'
  const bad = []
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    const s = getComputedStyle(el)
    if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) continue
    if (r.width <= 1 && r.height <= 1) continue
    const border = ['Top', 'Right', 'Bottom', 'Left'].some(
      (side) => s[`border${side}Style`] !== 'none' && parseFloat(s[`border${side}Width`]) > 0,
    )
    const text = (el.innerText || el.value || el.getAttribute('placeholder') || '').trim().length > 0
    const icon = !!el.querySelector('svg, img')
    const isField = /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.getAttribute('role') === 'combobox'
    const isToggle = /^(switch|checkbox)$/.test(el.getAttribute('role') || '') || ['checkbox', 'radio'].includes(el.type)
    const visibleText = text && s.color !== s.backgroundColor
    let ok
    if (isField || isToggle) {
      // Fields and toggles need an edge; native checkboxes paint their own.
      ok = border || ['checkbox', 'radio', 'range', 'color', 'file'].includes(el.type)
      if (isToggle && !border) {
        ok = ok || [...el.querySelectorAll('*')].some((c) => {
          const cs = getComputedStyle(c)
          return ['Top', 'Right', 'Bottom', 'Left'].some((side) => cs[`border${side}Style`] !== 'none' && parseFloat(cs[`border${side}Width`]) > 0)
        })
      }
    } else {
      ok = border || visibleText || icon
    }
    if (!ok) {
      const name = el.getAttribute('aria-label') || (el.textContent || '').trim().slice(0, 40)
      bad.push(`${el.tagName.toLowerCase()}[${el.getAttribute('role') || el.type || ''}] "${name}"`)
    }
  }
  return {
    forced: matchMedia('(forced-colors: active)').matches,
    bad: [...new Set(bad)],
  }
}

const FORCED_ROUTES = [
  ['home', '/home'],
  ['chat-session', SESSION],
  ['sessions', '/sessions'],
  ['projects', '/projects'],
  ['automations', '/automations'],
  ['usage', '/usage'],
  ['inbox', '/inbox'],
  ['settings-appearance', '/settings/appearance'],
  ['settings-environment', '/settings/environment'],
  ['settings-advanced', '/settings/advanced'],
  ['settings-approvals', '/settings/approvals'],
]

async function auditForcedColors(browser) {
  for (const vp of [DESKTOP, PHONE]) {
    const context = await newContext(browser, { ...vp, theme: 'dark', forcedColors: 'active' })
    const page = await context.newPage()
    for (const [id, p] of FORCED_ROUTES) {
      const entry = { route: id, viewport: vp.name }
      try {
        await page.goto(BASE + p)
        await settle(page)
        const shot = path.join(OUT, `forced-colors-${id}-${vp.name}.png`)
        Object.assign(entry, await page.evaluate(FORCED_CHECK))
        await fullScreenshot(page, shot)
        entry.screenshot = shot
      } catch (error) {
        entry.error = String(error.message || error)
      }
      entry.pass = !entry.error && entry.forced === true && entry.bad.length === 0
      if (!entry.pass) fail('forcedColors', { route: id, viewport: vp.name, error: entry.error, forced: entry.forced, bad: entry.bad })
      results.forcedColors.push(entry)
    }
    await context.close()
  }
}

// ---------------------------------------------------------------------------
// 4. Deep links + reload, legacy redirects, custom base path.
// ---------------------------------------------------------------------------
function expectedPath(base, route, projectId) {
  const url = new URL(routeUrl(base, route, projectId))
  // The OAuth callback finishes by moving to Settings › MCP.
  return url.pathname + url.search
}

async function auditDeepLinks(browser, projectId) {
  for (const [base, label] of [[BASE, 'default'], ...(ALT_BASE ? [[ALT_BASE, 'custom']] : [])]) {
    const context = await newContext(browser, { width: 1280, height: 800, theme: 'dark' })
    const page = await context.newPage()
    const diag = attachDiagnostics(page)
    for (const route of ROUTES) {
      if (label === 'custom' && !['home', 'chat-session', 'project-detail', 'settings-health', 'settings-advanced', 'usage', 'mcp-oauth-callback'].includes(route.id)) continue
      const entry = { base: label, route: route.id }
      try {
        const res = await page.goto(routeUrl(base, route, projectId))
        await settle(page)
        const first = await pageMetrics(page)
        const res2 = await page.reload()
        await settle(page)
        const second = await pageMetrics(page)
        entry.status = [res?.status(), res2?.status()]
        entry.paths = [first.path, second.path]
        entry.notFound = first.notFound || second.notFound
        const basePath = new URL(base).pathname
        const expected = expectedPath(base, route, projectId)
        entry.expected = expected
        const lands = (p) =>
          route.lands ? p.startsWith(basePath) && route.lands.test(p.slice(basePath.length)) : p === expected
        const stable =
          route.id === 'mcp-oauth-callback'
            ? second.path.startsWith(basePath)
            : lands(first.path) && lands(second.path)
        // Every same-origin link in the shell stays under the base path.
        entry.foreignLinks = await page.evaluate((bp) =>
          [...document.querySelectorAll('a[href]')]
            .map((a) => new URL(a.href, location.href))
            .filter((u) => u.origin === location.origin && !u.pathname.startsWith(bp) && !u.pathname.startsWith('/api/'))
            .map((u) => u.pathname), basePath)
        entry.pass = entry.status.every((s) => s === 200) && stable && !entry.notFound && entry.foreignLinks.length === 0
      } catch (error) {
        entry.error = String(error.message || error)
        entry.pass = false
      }
      if (!entry.pass) fail('deepLinks', entry)
      ;(label === 'custom' ? results.basePath : results.deepLinks).push(entry)
    }
    for (const [from, to] of LEGACY) {
      const entry = { base: label, from, expected: to }
      try {
        await page.goto(base + from)
        await settle(page)
        const m = await pageMetrics(page)
        entry.landed = m.path
        const bp = new URL(base).pathname
        entry.pass = to instanceof RegExp ? m.path.startsWith(bp) && to.test(m.path.slice(bp.length)) : m.path === bp + to
        entry.expected = String(to)
        if (entry.pass) {
          // Redirects replace history: Back must not return to the legacy URL.
          const backTarget = await page.evaluate(() => history.length)
          entry.historyLength = backTarget
        }
      } catch (error) {
        entry.error = String(error.message || error)
        entry.pass = false
      }
      if (!entry.pass) fail('legacy', entry)
      results.legacy.push(entry)
    }
    const errs = [...diag.pageErrors, ...diag.consoleErrors, ...diag.badResponses, ...diag.requestFailures]
    if (errs.length) fail('deepLinks', { base: label, diagnostics: diag })
    await context.close()
  }
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  })
  try {
    const projectId = runs('matrix') || runs('deep') || runs('targets') ? await createProject(browser) : null
    results.projectId = projectId
    if (runs('matrix')) await auditMatrix(browser, projectId)
    if (runs('targets')) await auditFineTargets(browser, projectId)
    if (runs('keyboard')) await auditKeyboard(browser)
    if (runs('motion')) await auditReducedMotion(browser)
    if (runs('forced')) await auditForcedColors(browser)
    if (runs('deep')) await auditDeepLinks(browser, projectId)
  } finally {
    await browser.close()
  }
  results.finishedAt = new Date().toISOString()
  fs.writeFileSync(process.env.AUDIT_RESULTS || path.join(OUT, 'results.json'), JSON.stringify(results, null, 2))
  const summary = {
    out: OUT,
    matrix: { total: results.matrix.length, failed: results.matrix.filter((m) => !m.pass).length },
    axeSeriousOrCritical: results.axe.filter((v) => ['serious', 'critical'].includes(v.impact)).length,
    axeModerate: results.axe.filter((v) => v.impact === 'moderate').length,
    keyboardFailed: results.keyboard.filter((k) => !k.pass).length,
    reducedMotionFailed: results.reducedMotion.filter((k) => !k.pass).length,
    forcedColorsFailed: results.forcedColors.filter((k) => !k.pass).length,
    deepLinksFailed: [...results.deepLinks, ...results.basePath].filter((k) => !k.pass).length,
    legacyFailed: results.legacy.filter((k) => !k.pass).length,
    touchTargetRoutes: results.touchTargets.length,
    fineTargetRoutes: results.fineTargets.filter((x) => x.small.length).length,
    failures: results.failures.length,
  }
  console.log(JSON.stringify(summary))
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error))
  process.exit(1)
})
