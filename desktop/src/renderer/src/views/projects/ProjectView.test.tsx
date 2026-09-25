import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useConnection } from '@/stores/connection'
import type { RawProject } from '@/views/projects/logic'
import { useGateway } from '~/stores/gateway'
import { ProjectView } from './ProjectView'

const rpcCall = vi.fn()
const rpc = { call: rpcCall, waitForConnection: async () => {}, on: () => () => {} }
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

const PROJECT: RawProject = {
  project_id: 'p1',
  agent_id: 'main',
  name: 'Roadmap',
  knowledge: '',
  created_at: 1_700_000_000_000,
  updated_at: 1_700_000_100_000,
}

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  })
  render(
    <MemoryRouter initialEntries={['/projects/p1']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/projects/:id" element={<ProjectView />} />
          <Route path="*" element={null} />
        </Routes>
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  rpcCall.mockReset()
  rpcCall.mockImplementation(async (method: string) => {
    switch (method) {
      case 'projects.list':
        return { projects: [PROJECT] }
      case 'sessions.list':
        return { sessions: [] }
      case 'projects.update':
        return { project: { ...PROJECT, name: 'Launch plan' } }
      case 'projects.delete':
        return { deleted: true }
      case 'sessions.create':
        return { key: 'agent:main:webchat:new' }
      default:
        return {}
    }
  })
  useConnection.getState().setState('connected')
  useGateway.setState({ status: { state: 'running', pid: 1, url: 'http://x', error: null } })
})

describe('ProjectView', () => {
  it('renames from the title, conflict-checked against the version on screen', async () => {
    mount()
    const title = await screen.findByRole('textbox', { name: 'Rename' })
    fireEvent.change(title, { target: { value: 'Launch plan' } })
    fireEvent.blur(title)
    await waitFor(() =>
      expect(rpcCall).toHaveBeenCalledWith('projects.update', {
        projectId: 'p1',
        name: 'Launch plan',
        expectedUpdatedAt: 1_700_000_100_000,
      }),
    )
  })

  it('starts a chat filed here and opens it', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'New chat' }))
    await waitFor(() =>
      expect(screen.getByTestId('where').textContent).toBe(
        `/sessions/${encodeURIComponent('agent:main:webchat:new')}`,
      ),
    )
    expect(rpcCall).toHaveBeenCalledWith('sessions.create', { agentId: 'main', projectId: 'p1' })
  })

  it('deletes behind its alert and leaves the page', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'More' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete project…' }))
    const alert = screen.getByRole('alertdialog', { name: 'Delete this project?' })
    fireEvent.click(within(alert).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/sessions'))
    expect(rpcCall).toHaveBeenCalledWith('projects.delete', { projectId: 'p1' })
  })
})
