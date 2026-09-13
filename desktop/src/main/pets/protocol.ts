import { net, protocol } from 'electron'
import { pathToFileURL } from 'node:url'
import { PET_SCHEME } from '@shared/pet'
import type { PetStore } from './store'

/**
 * `agentos-pet://sheet/<slug>` streams a spritesheet from the pets directory
 * straight into an <img>, so a 2 MB sheet never crosses IPC as base64 and the
 * renderer's image cache does its job. The renderer's CSP lists the scheme
 * under img-src; nothing else is reachable through it.
 */
export function registerPetScheme(): void {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: PET_SCHEME,
      // corsEnabled: the renderer loads sheets crossOrigin="anonymous" so it
      // may read their pixels back off a canvas (protocol.handle adds the ACAO header).
      privileges: { standard: true, secure: true, corsEnabled: true, supportFetchAPI: false },
    },
  ])
}

/** After `app.whenReady()`. */
export function servePets(store: PetStore): void {
  protocol.handle(PET_SCHEME, async (request) => {
    let url: URL
    try {
      url = new URL(request.url)
    } catch {
      return new Response('bad request', { status: 400 })
    }
    if (url.hostname !== 'sheet') return new Response('not found', { status: 404 })
    const slug = url.pathname.replace(/^\/+/, '')
    const file = store.sheetPath(slug)
    if (!file) return new Response('not found', { status: 404 })
    const res = await net.fetch(pathToFileURL(file).toString())
    // CORS-open: the renderer reads pixels back off a canvas to find each
    // row's real frames, which a cross-origin image would refuse (taint).
    return new Response(res.body, {
      status: res.status,
      headers: {
        'Content-Type': 'image/webp',
        'Cache-Control': 'max-age=3600',
        'Access-Control-Allow-Origin': '*',
      },
    })
  })
}
