const STATIC_DIST_SUFFIX = /\/static\/dist\/?$/

interface ControlBaseOptions {
  document?: Document
  documentUrl?: string
  buildBase?: string
}

interface ControlBaseInputs {
  documentUrl: string
  taggedControlBase?: string | null
  taggedBaseHref?: string | null
  buildBase: string
}

function normalizedPath(pathname: string): string {
  const withLeadingSlash = pathname.startsWith('/') ? pathname : `/${pathname}`
  const collapsed = withLeadingSlash.replace(/\/{2,}/g, '/').replace(/\/+$/, '')
  return collapsed || '/'
}

function urlPath(value: string, documentUrl: string): string | null {
  try {
    return new URL(value, documentUrl).pathname
  } catch {
    return null
  }
}

/**
 * Resolve the Control UI mount path independently from the asset directory.
 *
 * Production serves the relative Vite build with a tag shaped like:
 *
 *   <base
 *     href="/control/static/dist/"
 *     data-agentos-control-base="/control"
 *   >
 *
 * `href` gives the browser a stable asset base even on a deep link, while the
 * data attribute gives React Router and the bootstrap fetch their mount path.
 * A valueless data attribute is also supported: in that form the control path
 * is derived from `href` by removing the static build suffix.
 */
export function deriveControlBasePath({
  documentUrl,
  taggedControlBase,
  taggedBaseHref,
  buildBase,
}: ControlBaseInputs): string {
  const explicit = taggedControlBase?.trim()
  if (explicit) {
    const pathname = urlPath(explicit, documentUrl)
    if (pathname) return normalizedPath(pathname)
  }

  const taggedHref = taggedBaseHref?.trim()
  if (taggedHref) {
    const pathname = urlPath(taggedHref, documentUrl)
    if (pathname) return normalizedPath(pathname.replace(STATIC_DIST_SUFFIX, ''))
  }

  // Vite dev serves at /control/. Production uses "./" and therefore requires
  // the server-provided <base> above; retaining the build fallback keeps dev and
  // component tests independent from gateway HTML.
  if (buildBase !== '.' && buildBase !== './') {
    const pathname = urlPath(buildBase, documentUrl)
    if (pathname) return normalizedPath(pathname.replace(STATIC_DIST_SUFFIX, ''))
  }

  return '/'
}

export function controlBasePath(options: ControlBaseOptions = {}): string {
  const doc = options.document ?? document
  const documentUrl = options.documentUrl ?? window.location.href
  const tag = doc.querySelector<HTMLBaseElement>('base[data-agentos-control-base]')

  return deriveControlBasePath({
    documentUrl,
    taggedControlBase: tag?.getAttribute('data-agentos-control-base'),
    taggedBaseHref: tag?.getAttribute('href'),
    buildBase: options.buildBase ?? import.meta.env.BASE_URL,
  })
}

export function controlPath(path: string, basePath = controlBasePath()): string {
  const suffix = path.replace(/^\/+/, '')
  return basePath === '/' ? `/${suffix}` : `${basePath}/${suffix}`
}
