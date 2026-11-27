/**
 * An artifact's file, as the web console loads it.
 *
 * A transcript artifact names its file by a gateway URL
 * (`/api/v1/artifacts/<id>`). That route is the deprecated pre-ticket one: it
 * finds a file only through a session key, so requested as is it answers
 * 404, and an `<img>` or a link cannot send the token it wants either. Every
 * read here goes through a ticket instead (`artifacts.download`,
 * lib/artifact-tickets.ts): a URL bound to this socket that needs no header,
 * which `fetch`, an `<img>` and a download link can all use.
 */
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/webChat'
import { gatewayHttpReachable } from '@/lib/api-origin'
import {
  bindTicketResolver,
  createTicketResolver,
  parseDownloadUrl,
  type DownloadTarget,
  type TicketResolver,
} from '@/lib/artifact-tickets'
import { artifactDownloadUrl, artifactName, type Artifact } from '@/views/chat/transcript/artifacts'

/** The part of the RPC client a ticket needs. */
export interface TicketRpc {
  call(method: string, params: Record<string, unknown>): Promise<unknown>
  on(event: string, handler: (...args: unknown[]) => void): () => void
}

const resolvers = new WeakMap<TicketRpc, TicketResolver>()

/**
 * The ticket resolver of a connection: one per client, so the transcript,
 * its cards and the panel share tickets, and a reconnect drops them all.
 */
export function artifactTickets(rpc: TicketRpc): TicketResolver {
  let resolver = resolvers.get(rpc)
  if (!resolver) {
    resolver = createTicketResolver({
      call: (method, params) => rpc.call(method, params),
      // Every read names its session.
      getSessionKey: () => '',
    })
    bindTicketResolver(rpc, resolver)
    resolvers.set(rpc, resolver)
  }
  return resolver
}

/** The file an artifact names, or null when it names none. */
export function artifactTarget(artifact: Artifact): DownloadTarget | null {
  if (artifact.id) return { kind: 'artifact', id: String(artifact.id) }
  const url = artifactDownloadUrl(artifact)
  return url ? parseDownloadUrl(url) : null
}

export interface ArtifactTicketOptions {
  /** A still image's PNG preview instead of the file. */
  thumbnail?: boolean
  /** Mint a new ticket even when one is cached. */
  fresh?: boolean
}

/**
 * A ticket URL for the artifact's file in `sessionKey`. Rejects with the
 * gateway's refusal (`artifact not found`, …), or when no ticket can be had:
 * the socket is down, or the gateway mints none.
 */
export async function artifactTicketUrl(
  rpc: TicketRpc,
  artifact: Artifact,
  sessionKey: string,
  options: ArtifactTicketOptions = {},
): Promise<string> {
  const target = artifactTarget(artifact)
  if (!target) throw new Error(t('webChat.artifactNoFile'))
  const url = await artifactTickets(rpc).resolve(target, { ...options, sessionKey })
  if (!url) throw new Error(t('webChat.artifactNoTicket'))
  return url
}

/** The artifact's file over HTTP; rejects unless the gateway answered 2xx. */
export async function fetchArtifact(
  rpc: TicketRpc,
  artifact: Artifact,
  sessionKey: string,
): Promise<Response> {
  // HTTP reaches only the gateway that served this page (the console's CSP).
  if (!gatewayHttpReachable()) throw new Error(t('webChat.artifactOtherGateway'))
  const load = async (fresh: boolean) =>
    fetch(await artifactTicketUrl(rpc, artifact, sessionKey, { fresh }), {
      credentials: 'same-origin',
    })
  let response = await load(false)
  // A cached ticket expired or was evicted since it was minted: one fresh try.
  if (response.status === 404) response = await load(true)
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response
}

/** Save the artifact's file: a fresh ticket, which the browser downloads itself. */
export async function downloadArtifact(
  rpc: TicketRpc,
  artifact: Artifact,
  sessionKey: string,
): Promise<void> {
  // Fresh: a cached ticket that died meanwhile would download an error page.
  const url = await artifactTicketUrl(rpc, artifact, sessionKey, { fresh: true })
  const link = document.createElement('a')
  link.href = url
  link.download = artifactName(artifact)
  link.rel = 'noopener'
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

/** Save an artifact's file from a click, saying why when it cannot be had. */
export function useArtifactDownload(sessionKey: string): (artifact: Artifact) => Promise<void> {
  const rpc = useRpc()
  return (artifact) =>
    downloadArtifact(rpc, artifact, sessionKey).catch((error: unknown) => {
      toast.error(
        t('webChat.artifactDownloadFailed', {
          name: artifactName(artifact),
          message: error instanceof Error ? error.message : String(error),
        }),
      )
    })
}

export interface ArtifactImage {
  /** The ticket URL to show; null while it is minted, or once it failed. */
  src: string | null
  failed: boolean
  /** The `<img>`'s `onError`: one retry with a fresh ticket for the full file. */
  onError(): void
}

/**
 * An image artifact's source for an `<img>`. A ticket that stops loading has
 * expired, lost its socket, or (for a preview) names a file with no preview:
 * it is retried once, fresh and for the full file, as the transcript does.
 */
export function useArtifactImage(
  artifact: Artifact,
  sessionKey: string,
  thumbnail = false,
): ArtifactImage {
  const rpc = useRpc()
  const [retried, setRetried] = useState(false)
  const [state, setState] = useState<{ src: string | null; failed: boolean }>({
    src: null,
    failed: false,
  })
  useEffect(() => {
    let live = true
    artifactTicketUrl(rpc, artifact, sessionKey, {
      thumbnail: thumbnail && !retried,
      fresh: retried,
    }).then(
      (src) => live && setState({ src, failed: false }),
      () => live && setState({ src: null, failed: true }),
    )
    return () => {
      live = false
    }
  }, [rpc, artifact, sessionKey, thumbnail, retried])
  return {
    ...state,
    onError: () => {
      if (retried) setState({ src: null, failed: true })
      else setRetried(true)
    },
  }
}
