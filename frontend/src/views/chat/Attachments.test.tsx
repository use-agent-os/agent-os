import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Attachments, useAttachments, type UseAttachments } from './Attachments'
import { LARGE_PASTE_CHARS, PAGE_DUMP_CHARS, hasPendingAttachmentWork } from './logic'

// The attachment tray is a real React component (idiomatic, not the imperative
// legacy `_renderAttachmentPreview`). `useAttachments` owns the pending buffer +
// the FileReader / staged-upload lifecycle (chat.js:8052-8161); `<Attachments>`
// renders the previews + cap-rejection message. These RTL tests exercise both.

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}))

// A File-like whose FileReader-readable content is a stable data URL. jsdom's
// FileReader.readAsDataURL yields a base64 of the blob's bytes, so a real File
// works; we keep a helper so the mime/size are explicit.
function makeFile(name: string, type: string, size: number, contents = 'data'): File {
  const blob = new Blob([contents], { type })
  const file = new File([blob], name, { type })
  // jsdom sizes the File from its parts; override to the declared size so cap
  // tests can exercise oversized files without allocating megabytes.
  Object.defineProperty(file, 'size', { value: size, configurable: true })
  return file
}

// A tiny harness that mounts the hook + tray together and republishes the hook
// API on every render so tests can drive addFiles / read fresh state without a
// full page. (Publishing during render is fine here: onReady only stores the
// latest api reference into a test-scoped variable, no React state is touched.)
function Harness({ onReady }: { onReady: (api: UseAttachments) => void }) {
  const api = useAttachments()
  onReady(api)
  return <Attachments api={api} />
}

let api: UseAttachments
function renderTray() {
  render(<Harness onReady={(a) => (api = a)} />)
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('Attachments tray', () => {
  it('accepts an image file and shows a preview thumbnail (chat.js:8069/8359)', async () => {
    renderTray()
    await act(async () => {
      api.addFiles([makeFile('photo.png', 'image/png', 1234)])
    })
    // The inline read resolves → a thumbnail img with the file name as alt.
    await waitFor(() => {
      expect(screen.getByRole('img', { name: 'photo.png' })).toBeInTheDocument()
    })
    expect(api.attachments).toHaveLength(1)
    expect(api.attachments[0]?.mime).toBe('image/png')
  })

  it('accepts a text file and shows a file chip (chat.js:8069/8370)', async () => {
    renderTray()
    await act(async () => {
      api.addFiles([makeFile('notes.md', 'text/markdown', 500, '# hi')])
    })
    await waitFor(() => {
      expect(screen.getByText('notes.md')).toBeInTheDocument()
    })
    // Chip, not an image thumbnail.
    expect(screen.queryByRole('img', { name: 'notes.md' })).not.toBeInTheDocument()
  })

  it('rejects an oversized file inline with the allowed-types label (chat.js:8059)', async () => {
    renderTray()
    await act(async () => {
      // 6 MB image > the 5 MB image cap.
      api.addFiles([makeFile('huge.png', 'image/png', 6 * 1024 * 1024)])
    })
    expect(api.attachments).toHaveLength(0)
    // The rejection surfaces inline in the tray with the byte cap.
    expect(screen.getByText(/File too large/i)).toBeInTheDocument()
    expect(screen.getByText(/huge\.png/)).toBeInTheDocument()
  })

  it('rejects an unsupported mime with the allowed-types label (chat.js:8055)', async () => {
    renderTray()
    await act(async () => {
      api.addFiles([makeFile('virus.exe', 'application/x-msdownload', 10)])
    })
    expect(api.attachments).toHaveLength(0)
    expect(screen.getByText(/Unsupported file/i)).toBeInTheDocument()
    expect(
      screen.getByText(/PNG, JPEG, GIF, WEBP, PDF, TXT, MD, HTML, CSV, JSON/),
    ).toBeInTheDocument()
  })

  it('removes a pending attachment when its remove button is clicked (chat.js:8379)', async () => {
    renderTray()
    await act(async () => {
      api.addFiles([makeFile('a.png', 'image/png', 100)])
    })
    await waitFor(() => expect(api.attachments).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: /remove attachment a\.png/i }))
    await waitFor(() => expect(api.attachments).toHaveLength(0))
  })
})

describe('useAttachments paste normalization', () => {
  it('converts a >=20k paste into a generated .txt attachment (chat.js:8017)', async () => {
    renderTray()
    const paste = 'x'.repeat(LARGE_PASTE_CHARS)
    let normalized: Awaited<ReturnType<typeof api.normalizeForSend>>
    await act(async () => {
      normalized = await api.normalizeForSend(paste, false)
    })
    expect(normalized!.text).toBe('Please process the attached pasted text.')
    expect(normalized!.attachments.some((a) => a.generated)).toBe(true)
  })

  it('converts a >=8k page dump into a page-dump attachment (chat.js:8035)', async () => {
    renderTray()
    const body =
      'CHAT SESSION agent:main:webchat: Still waiting for agent response\n' +
      'z'.repeat(PAGE_DUMP_CHARS)
    let normalized: Awaited<ReturnType<typeof api.normalizeForSend>>
    await act(async () => {
      normalized = await api.normalizeForSend(body, false)
    })
    expect(normalized!.text).toBe('Please process the attached WebChat page dump.')
    expect(normalized!.normalized?.kind).toBe('page_dump')
  })
})

describe('useAttachments send-enable + pending-work', () => {
  it('reports pending work while a file is being read (chat.js:8067)', async () => {
    renderTray()
    // Add a file but do not flush the FileReader; the entry starts inline_pending.
    act(() => {
      api.addFiles([makeFile('a.png', 'image/png', 100)])
    })
    // Immediately after add, the entry is inline_pending → pending work is true.
    expect(hasPendingAttachmentWork(api.attachments)).toBe(true)
    // After the read resolves it flips to a resolved inline entry.
    await waitFor(() => expect(hasPendingAttachmentWork(api.attachments)).toBe(false))
  })
})
