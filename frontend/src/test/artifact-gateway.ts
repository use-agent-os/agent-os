// A stand-in for the gateway's artifact routes, for the web console's tests.
//
// It answers what the real gateway answers (gateway/download_grants.py,
// gateway/artifacts.py), so a test fails the way the console would:
// - `artifacts.download` mints a ticket for a file of the session it names,
//   and refuses (NOT_FOUND) a file that session does not own;
// - a ticket URL serves the file with no header, until its socket's tickets die;
// - the bare `/api/v1/artifacts/<id>` a transcript carries is the deprecated
//   route: without a session key it resolves nothing (404), and with auth on
//   it wants the token first (401). Every request to it is recorded, so a test
//   can say the console never used it.
import { vi } from 'vitest'
import { RpcError } from '@/lib/ws-rpc'
import type { FakeRpc } from './fake-rpc'

interface StoredFile {
  name: string
  mime: string
  body: string
  sessionKey: string
}

const TICKET_PATH = /^\/api\/v1\/artifacts\/download\/([^/]+)\/([^/]+)$/
const LEGACY_PATH = /^\/api\/v1\/artifacts\/([^/]+)$/

function rpcError(code: string, message: string): RpcError {
  const error = new RpcError(message)
  error.code = code
  return error
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

export class FakeArtifactGateway {
  readonly files = new Map<string, StoredFile>()
  /** ticket → the artifact it opens. */
  readonly tickets = new Map<string, string>()
  /** Every artifact request, path and query. */
  readonly requests: string[] = []
  /** Requests to the deprecated `/api/v1/artifacts/<id>` route. */
  readonly legacyRequests: string[] = []
  private minted = 0

  constructor(
    readonly sessionKey: string,
    private readonly options: { token?: string; connId?: string } = {},
  ) {}

  /** Publish a file into `sessionKey` (default: the gateway's own session). */
  put(
    id: string,
    body: unknown,
    file: { name?: string; mime?: string; sessionKey?: string } = {},
  ): this {
    this.files.set(id, {
      name: file.name ?? id,
      mime: file.mime ?? 'application/json',
      body: typeof body === 'string' ? body : JSON.stringify(body),
      sessionKey: file.sessionKey ?? this.sessionKey,
    })
    return this
  }

  /** The artifact a transcript row carries for a published file. */
  artifact(id: string): Record<string, unknown> {
    const file = this.files.get(id)
    return {
      id,
      name: file?.name ?? id,
      mime: file?.mime ?? 'application/json',
      size: file?.body.length ?? 0,
      download_url: `/api/v1/artifacts/${id}`,
    }
  }

  /** Answer `artifacts.download` on `rpc` and stand in for `fetch`. */
  install(rpc: FakeRpc): this {
    rpc.handle('artifacts.download', (params: Record<string, unknown>) => this.mint(params))
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => this.fetch(input, init)),
    )
    return this
  }

  /** The socket that minted every live ticket closed: they all stop working. */
  dropTickets(): void {
    this.tickets.clear()
  }

  ticketUrls(): string[] {
    return this.requests.filter((r) => TICKET_PATH.test(r.split('?')[0]!))
  }

  private mint(params: Record<string, unknown>) {
    const sessionKey = String(params.session_key ?? params.sessionKey ?? '')
    const id = String(params.artifact_id ?? params.artifactId ?? '')
    if (!sessionKey) throw rpcError('INVALID_REQUEST', 'params.session_key is required')
    const file = this.files.get(id)
    if (!file || file.sessionKey !== sessionKey) throw rpcError('NOT_FOUND', 'artifact not found')
    const ticket = `ticket-${++this.minted}`
    this.tickets.set(ticket, id)
    return {
      url: `/api/v1/artifacts/download/${this.options.connId ?? 'conn-1'}/${ticket}`,
      kind: 'artifact',
      name: file.name,
      mime: file.mime,
      size: file.body.length,
      expiresAt: Date.now() + 300_000,
      ttlSeconds: 300,
    }
  }

  private async fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const raw = input instanceof Request ? input.url : String(input)
    const url = new URL(raw, window.location.href)
    this.requests.push(url.pathname + url.search)
    const headers = new Headers(init?.headers)

    const ticket = TICKET_PATH.exec(url.pathname)
    if (ticket) {
      const id = this.tickets.get(decodeURIComponent(ticket[2]!))
      const file = id ? this.files.get(id) : undefined
      if (!file) return jsonResponse(404, { error: 'Download not found', code: 'NOT_FOUND' })
      if (url.searchParams.get('variant') === 'thumbnail' && !file.mime.startsWith('image/')) {
        return jsonResponse(415, { error: 'No thumbnail for this file', code: 'INVALID_REQUEST' })
      }
      return new Response(file.body, {
        status: 200,
        headers: { 'content-type': file.mime, 'content-disposition': 'attachment' },
      })
    }

    const legacy = LEGACY_PATH.exec(url.pathname)
    if (legacy) {
      this.legacyRequests.push(url.pathname + url.search)
      const token = this.options.token
      if (token && headers.get('authorization') !== `Bearer ${token}`) {
        return jsonResponse(401, { error: 'Unauthorized', code: 'UNAUTHORIZED' })
      }
      const sessionKey =
        url.searchParams.get('sessionKey') ||
        url.searchParams.get('session_key') ||
        headers.get('x-agentos-session-key') ||
        ''
      const file = this.files.get(decodeURIComponent(legacy[1]!))
      if (!file || !sessionKey || file.sessionKey !== sessionKey) {
        return jsonResponse(404, { error: 'Artifact not found', code: 'NOT_FOUND' })
      }
      return new Response(file.body, { status: 200, headers: { 'content-type': file.mime } })
    }
    return jsonResponse(404, { error: 'Not found' })
  }
}
