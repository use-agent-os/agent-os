import '@/test/web'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FakeArtifactGateway } from '@/test/artifact-gateway'
import { KEY, assistantRow, chatRpc, openChat, resetChatState, userRow } from '@/test/web-chat'

const TICKET = /^\/api\/v1\/artifacts\/download\/conn-1\/ticket-\d+$/

const log = () => screen.getByRole('log')
const panel = () => screen.getByRole('complementary', { name: 'Workspace panel' })

/** A chat whose reply carries `files`, served by a ticket-only gateway. */
function filesChat(files: { id: string; name: string; mime: string; body: string }[]) {
  const gateway = new FakeArtifactGateway(KEY)
  for (const f of files) gateway.put(f.id, f.body, { name: f.name, mime: f.mime })
  const rpc = chatRpc([
    userRow('u1', 'files'),
    assistantRow('a1', 'Here.', { artifacts: files.map((f) => gateway.artifact(f.id)) }),
  ])
  gateway.install(rpc)
  return { rpc, gateway }
}

/** What a download click handed the browser. */
function captureDownloads(): { href: string; download: string }[] {
  const clicked: { href: string; download: string }[] = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push({ href: this.getAttribute('href') ?? '', download: this.download })
  })
  return clicked
}

beforeEach(resetChatState)
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('artifact chips', () => {
  it("shows an image's preview through a ticket, not the bare artifact URL", async () => {
    const { rpc, gateway } = filesChat([
      { id: 'img1', name: 'render.png', mime: 'image/png', body: 'png' },
    ])
    await openChat(rpc)
    const img = await waitFor(() => {
      const el = log().querySelector<HTMLImageElement>('.chat-artifact img')
      if (!el?.getAttribute('src')) throw new Error('no preview yet')
      return el
    })
    const src = new URL(img.getAttribute('src')!, window.location.href)
    expect(src.pathname).toMatch(TICKET)
    expect(src.searchParams.get('variant')).toBe('thumbnail')
    expect(rpc.callsTo('artifacts.download')).toEqual([
      { session_key: KEY, artifact_id: 'img1', transport: 'http' },
    ])
    expect(gateway.legacyRequests).toEqual([])
  })

  it('falls back to the full image, then to the file tile, when a preview will not load', async () => {
    const { rpc } = filesChat([{ id: 'img1', name: 'anim.gif', mime: 'image/gif', body: 'gif' }])
    await openChat(rpc)
    const img = await waitFor(() => {
      const el = log().querySelector<HTMLImageElement>('.chat-artifact img')
      if (!el?.getAttribute('src')) throw new Error('no preview yet')
      return el
    })
    img.dispatchEvent(new Event('error'))
    await waitFor(() => {
      const next = log().querySelector<HTMLImageElement>('.chat-artifact img')
      expect(next?.getAttribute('src')).toMatch(/ticket-2$/)
    })
    log().querySelector('.chat-artifact img')!.dispatchEvent(new Event('error'))
    await waitFor(() => expect(log().querySelector('.chat-artifact img')).toBeNull())
    expect(rpc.callsTo('artifacts.download')).toHaveLength(2)
  })

  it('downloads through a fresh ticket under the artifact name', async () => {
    const user = userEvent.setup()
    const clicked = captureDownloads()
    const { rpc, gateway } = filesChat([
      { id: 'csv1', name: 'table.csv', mime: 'text/csv', body: 'a,b\n1,2\n' },
    ])
    await openChat(rpc)
    await user.click(await within(log()).findByRole('button', { name: 'Download table.csv' }))
    await waitFor(() => expect(clicked).toHaveLength(1))
    expect(clicked[0]!.download).toBe('table.csv')
    expect(new URL(clicked[0]!.href, window.location.href).pathname).toMatch(TICKET)
    expect(gateway.legacyRequests).toEqual([])
  })
})

describe('artifacts in the workspace panel', () => {
  const FILES = [
    { id: 'img1', name: 'render.png', mime: 'image/png', body: 'png' },
    { id: 'md1', name: 'notes.md', mime: 'text/markdown', body: '# Seeded notes\n\n- **two**\n' },
    { id: 'txt1', name: 'log.txt', mime: 'text/plain', body: 'plain text artifact' },
    { id: 'html1', name: 'page.html', mime: 'text/html', body: '<h1>Seeded HTML</h1>' },
    { id: 'pdf1', name: 'report.pdf', mime: 'application/pdf', body: '%PDF-1.4' },
  ]

  async function open(name: string) {
    const user = userEvent.setup()
    await user.click(await within(log()).findByRole('button', { name: `Open ${name}` }))
    return user
  }

  it('shows an image through a ticket for the full file', async () => {
    const { rpc } = filesChat(FILES)
    await openChat(rpc)
    await open('render.png')
    const img = await waitFor(() => {
      const el = panel().querySelector<HTMLImageElement>('.ws-item img')
      if (!el?.getAttribute('src')) throw new Error('no image yet')
      return el
    })
    const src = new URL(img.getAttribute('src')!, window.location.href)
    expect(src.pathname).toMatch(TICKET)
    expect(src.search).toBe('')
  })

  it('renders Markdown and plain text it read through a ticket', async () => {
    const { rpc, gateway } = filesChat(FILES)
    await openChat(rpc)
    await open('notes.md')
    expect(
      await within(panel()).findByRole('heading', { name: 'Seeded notes' }),
    ).toBeInTheDocument()
    await open('log.txt')
    expect(await within(panel()).findByText('plain text artifact')).toBeInTheDocument()
    expect(gateway.ticketUrls()).toHaveLength(2)
    expect(gateway.legacyRequests).toEqual([])
    // A frame cannot show a ticket: it answers as an attachment.
    expect(panel().querySelector('iframe[src]')).toBeNull()
  })

  it('shows an HTML file in a sandboxed frame from what it read', async () => {
    const { rpc } = filesChat(FILES)
    await openChat(rpc)
    await open('page.html')
    const frame = await waitFor(() => {
      const el = panel().querySelector('iframe')
      if (!el) throw new Error('no frame yet')
      return el
    })
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('srcdoc')).toBe('<h1>Seeded HTML</h1>')
    expect(frame.hasAttribute('src')).toBe(false)
  })

  it('offers any other file as a download through a ticket', async () => {
    const clicked = captureDownloads()
    const { rpc } = filesChat(FILES)
    await openChat(rpc)
    const user = await open('report.pdf')
    await user.click(await within(panel()).findByRole('button', { name: 'report.pdf' }))
    await waitFor(() => expect(clicked).toHaveLength(1))
    expect(clicked[0]!.download).toBe('report.pdf')
    expect(new URL(clicked[0]!.href, window.location.href).pathname).toMatch(TICKET)
  })

  it('says why a text file did not load and retries it', async () => {
    const { rpc, gateway } = filesChat(FILES)
    gateway.files.delete('txt1')
    await openChat(rpc)
    const user = await open('log.txt')
    expect(
      await within(panel()).findByText("Couldn't load this file: artifact not found"),
    ).toBeInTheDocument()
    gateway.put('txt1', 'back again', { name: 'log.txt', mime: 'text/plain' })
    await user.click(within(panel()).getByRole('button', { name: 'Retry' }))
    expect(await within(panel()).findByText('back again')).toBeInTheDocument()
  })
})
