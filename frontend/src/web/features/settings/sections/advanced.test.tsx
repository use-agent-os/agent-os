import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'
import { useRestartNotice } from '../config-editor'

// Settings › Advanced: the config.patch form and the config.apply YAML editor.

beforeEach(() => {
  resetShellState()
  useRestartNotice.getState().clear()
})

const CONFIG = {
  mode: 'a',
  agent_timeout_context_recovery: null,
  llm: { api_key: '', base_url: 'https://proxy.example/v1' },
  routing: { order: ['a', 'b'] },
}

const SCHEMA = {
  properties: {
    mode: { type: 'string', enum: ['a', 'b'] },
    agent_timeout_context_recovery: {
      anyOf: [{ type: 'boolean' }, { type: 'null' }],
      title: 'Agent Timeout Context Recovery',
      description: 'Unset: on.',
    },
    llm: { $ref: '#/$defs/Llm' },
    routing: { $ref: '#/$defs/Routing' },
  },
  $defs: {
    Llm: {
      properties: {
        api_key: { type: 'string' },
        base_url: { anyOf: [{ type: 'string' }, { type: 'null' }] },
      },
    },
    Routing: { properties: { order: { type: 'array', items: { type: 'string' } } } },
  },
}

function advancedRpc(
  snapshot: () => Record<string, unknown> = () => ({
    config: CONFIG,
    revision: 'rev-1',
  }),
) {
  return shellRpc()
    .handle('config.snapshot', snapshot)
    .handle('config.schema', { schema: SCHEMA })
    .handle('config.patch', { patched: [], restartRequired: false })
    .handle('config.apply', { restartRequired: false })
}

async function open(rpc = advancedRpc()) {
  const utils = renderShell('/settings/advanced', rpc)
  await screen.findByRole('combobox', { name: 'mode' })
  return utils
}

const yamlTab = () => screen.getByRole('tab', { name: 'YAML' })
const yamlBox = () => screen.getByRole('textbox', { name: 'YAML' })

describe('Advanced › YAML', () => {
  it('applies the edited YAML with its baseline and revision', async () => {
    const user = userEvent.setup()
    const rpc = advancedRpc()
    await open(rpc)
    await user.click(yamlTab())
    const baseline = (yamlBox() as HTMLTextAreaElement).value
    await user.type(yamlBox(), '\nextra: 1')
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(rpc.callsTo('config.apply')).toEqual([
        {
          config_yaml: `${baseline}\nextra: 1`,
          baseline_yaml: baseline,
          expectedRevision: 'rev-1',
        },
      ]),
    )
  })

  it('keeps an in-progress YAML edit across a refetch that changed the config, and says so', async () => {
    const user = userEvent.setup()
    let current: Record<string, unknown> = { config: CONFIG, revision: 'rev-1' }
    const { queryClient } = await open(advancedRpc(() => current))
    await user.click(yamlTab())
    await user.type(yamlBox(), '\nmine: 1')
    current = { config: { ...CONFIG, mode: 'b' }, revision: 'rev-2' }
    await act(() => queryClient.invalidateQueries())
    expect(
      await screen.findByText(/The configuration changed since you started editing this YAML/),
    ).toBeInTheDocument()
    expect((yamlBox() as HTMLTextAreaElement).value).toMatch(/mine: 1$/)
    await user.click(screen.getByRole('button', { name: 'Discard and load the new configuration' }))
    expect((yamlBox() as HTMLTextAreaElement).value).toMatch(/mode: b/)
  })

  it('will not apply YAML over pending form edits', async () => {
    const user = userEvent.setup()
    await open()
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(yamlTab())
    await user.type(yamlBox(), '\nx: 1')
    expect(screen.getByText(/Save or discard the pending form changes/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled()
  })

  it('guards leaving with an unapplied YAML draft', async () => {
    const user = userEvent.setup()
    const { router } = await open()
    await user.click(yamlTab())
    await user.type(yamlBox(), '\nx: 1')
    await act(() => router.navigate('/home'))
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave with unsaved changes?' })
    // The bar cannot save YAML: only Keep editing or Discard.
    expect(within(dialog).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/home'))
  })
})

describe('Advanced › form', () => {
  it('keeps the leave guard for form edits while the YAML tab is showing', async () => {
    const user = userEvent.setup()
    const { router } = await open()
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(yamlTab())
    await act(() => router.navigate('/home'))
    expect(
      await screen.findByRole('alertdialog', { name: 'Leave with unsaved changes?' }),
    ).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/settings/advanced')
  })

  it('Discard drops the edits and the save bar', async () => {
    const user = userEvent.setup()
    await open()
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.getByRole('combobox', { name: 'mode' })).toHaveValue('a')
    expect(screen.queryByTestId('unsaved-count')).not.toBeInTheDocument()
  })

  it('blocks Save while a JSON field does not parse, saves it once it does', async () => {
    const user = userEvent.setup()
    const rpc = advancedRpc()
    await open(rpc)
    const json = screen.getByRole('textbox', { name: 'routing.order' })
    await user.clear(json)
    await user.type(json, '[["a", ')
    expect(
      screen.getByText(/Fix the invalid JSON before saving: routing.order/),
    ).toBeInTheDocument()
    expect(json).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    await user.type(json, '"c"]')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpc.callsTo('config.patch')).toEqual([
        { patches: { 'routing.order': ['a', 'c'] }, expectedRevision: 'rev-1' },
      ]),
    )
  })

  it('Discard resets the JSON text to the saved value', async () => {
    const user = userEvent.setup()
    await open()
    const json = screen.getByRole('textbox', { name: 'routing.order' })
    await user.clear(json)
    await user.type(json, '[["a", ')
    await user.click(screen.getByRole('button', { name: 'Discard' }))
    expect(JSON.parse((json as HTMLTextAreaElement).value)).toEqual(['a', 'b'])
    expect(screen.queryByTestId('unsaved-count')).not.toBeInTheDocument()
  })

  it('can set an optional boolean and return it to unset (null)', async () => {
    const user = userEvent.setup()
    const rpc = advancedRpc(() => ({
      config: { ...CONFIG, agent_timeout_context_recovery: false },
      revision: 'rev-1',
    }))
    await open(rpc)
    const select = screen.getByRole('combobox', { name: /^agent_timeout_context_recovery/ })
    expect(
      within(select)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Default', 'On', 'Off'])
    expect(select).toHaveValue('false')
    await user.selectOptions(select, '')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpc.callsTo('config.patch')).toEqual([
        { patches: { agent_timeout_context_recovery: null }, expectedRevision: 'rev-1' },
      ]),
    )
  })

  it('writes an emptied optional text field as null, not ""', async () => {
    const user = userEvent.setup()
    const rpc = advancedRpc()
    await open(rpc)
    await user.clear(screen.getByRole('textbox', { name: 'llm.base_url' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpc.callsTo('config.patch')).toEqual([
        { patches: { 'llm.base_url': null }, expectedRevision: 'rev-1' },
      ]),
    )
  })

  it('masks a typed secret in the review', async () => {
    const user = userEvent.setup()
    await open()
    await user.type(screen.getByLabelText('llm.api_key'), 'sk-very-secret')
    await user.click(screen.getByRole('button', { name: 'Review changes' }))
    const review = await screen.findByTestId('pending-review')
    expect(review).toHaveTextContent('llm.api_key')
    expect(review).not.toHaveTextContent('sk-very-secret')
  })

  it('raises a conflict when the revision moves under unsaved edits', async () => {
    const user = userEvent.setup()
    let current: Record<string, unknown> = { config: CONFIG, revision: 'rev-1' }
    const rpc = advancedRpc(() => current)
    const { queryClient } = await open(rpc)
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    current = { config: CONFIG, revision: 'rev-2' }
    await act(() => queryClient.invalidateQueries())
    expect(
      await screen.findByText('The configuration changed since you opened it'),
    ).toBeInTheDocument()
    // Save still names the revision the edit was made against.
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(rpc.callsTo('config.patch')).toEqual([
        { patches: { mode: 'b' }, expectedRevision: 'rev-1' },
      ]),
    )
  })

  it('explains a write-blocked config, disables editing and reloads on request', async () => {
    const user = userEvent.setup()
    const rpc = advancedRpc(() => ({
      config: CONFIG,
      revision: null,
      diskDiverged: true,
      writeBlocked: true,
    }))
    await open(rpc)
    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent('The config file changed on disk')
    expect(screen.getByRole('combobox', { name: 'mode' })).toBeDisabled()
    const before = rpc.callsTo('config.snapshot').length
    await user.click(within(notice).getByRole('button', { name: 'Reload' }))
    await waitFor(() => expect(rpc.callsTo('config.snapshot').length).toBeGreaterThan(before))
  })
})
