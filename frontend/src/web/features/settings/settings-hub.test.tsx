import '@/test/web'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderShell, resetShellState, shellRpc } from '@/test/web-shell'
import { useRestartNotice } from './config-editor'
import { restartReasonLabel } from './SettingsPage'

beforeEach(() => {
  resetShellState()
  useRestartNotice.getState().clear()
})

const CONFIG = {
  mode: 'a',
  cron: { failure_webhook: 'https://hooks.example/x' },
  custom_unknown: 7,
  agentos_router: { enabled: true },
}

const SCHEMA = {
  properties: {
    mode: {
      anyOf: [{ type: 'string', enum: ['a', 'b', 'c'] }, { type: 'null' }],
      description: 'How the gateway runs',
    },
    cron: { $ref: '#/$defs/Cron' },
    agentos_router: { $ref: '#/$defs/Router' },
  },
  $defs: {
    Cron: {
      properties: {
        failure_webhook: {
          anyOf: [{ type: 'string' }, { type: 'null' }],
          title: 'Cron failure webhook',
          description: 'Where failed runs are posted',
        },
      },
    },
    Router: { properties: { enabled: { type: 'boolean', description: 'Route each turn' } } },
  },
}

function settingsRpc(patch?: (p: Record<string, unknown>) => unknown) {
  return shellRpc()
    .handle('config.snapshot', { config: CONFIG, revision: 'rev-1' })
    .handle('config.schema', { schema: SCHEMA })
    .handle('config.patch', patch ?? { ok: true })
    .handle('onboarding.status', { needsOnboarding: false })
}

// Advanced rows are named by their dotted path, the schema title after it.
const HOOK = 'cron.failure_webhook Cron failure webhook'

async function openAdvanced(rpc = settingsRpc()) {
  const utils = renderShell('/settings/advanced', rpc)
  await screen.findByRole('combobox', { name: 'mode' })
  return utils
}

describe('settings hub', () => {
  it('lists the groups and sections in order', async () => {
    renderShell('/settings', settingsRpc().handle('onboarding.catalog', {}))
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    const groups = within(nav)
      .getAllByRole('region')
      .map((g) => [
        g.getAttribute('aria-label'),
        within(g)
          .getAllByRole('link')
          .map((l) => l.textContent),
      ])
    expect(groups).toEqual([
      ['General', ['Appearance', 'Notifications']],
      ['Connections', ['Gateway', 'Channels']],
      ['Agents', ['Agents', 'Models & routing', 'Search', 'Skills', 'MCP', 'Memory', 'Media']],
      ['Security', ['Approvals', 'Environment']],
      ['System', ['Health', 'Logs', 'Advanced', 'Debug', 'Updates', 'About']],
    ])
  })

  it('opens a section by deep link and marks it current', async () => {
    renderShell('/settings/environment', settingsRpc())
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    expect(within(nav).getByRole('link', { name: 'Environment' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Environment' }),
    ).toBeInTheDocument()
  })

  it('on phones shows one section at a time with a way back (the list squeezed it to 0 px)', async () => {
    renderShell('/settings/environment', settingsRpc())
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    expect(nav.closest('aside')?.className).toMatch(/max-md:hidden/)
    const back = screen.getByRole('link', { name: 'All settings' })
    expect(back).toHaveAttribute('href', '/settings')
    expect(back.className).toMatch(/md:hidden/)
    // The pane only scrolls on its own from md up; on phones the page scrolls.
    const pane = screen.getByRole('region', { name: 'Environment settings' })
    expect(pane.className).not.toMatch(/(^|\s)(scroll-contained|overflow-auto)(\s|$)/)
  })

  it('keeps the section pane keyboard-scrollable even with nothing to Tab to', async () => {
    renderShell('/settings/environment', settingsRpc())
    const pane = await screen.findByRole('region', { name: 'Environment settings' })
    expect(pane).toHaveAttribute('tabindex', '0')
  })
})

describe('settings summary', () => {
  it('shows the active provider and model, a pending restart and paused writes', async () => {
    const user = userEvent.setup()
    let model = 'openai/gpt-6-luna'
    const rpc = settingsRpc().handle('config.snapshot', () => ({
      config: { llm: { provider: 'openrouter', model } },
      revision: null,
      pendingRestart: true,
      restartReasons: ['provider'],
      writeBlocked: true,
    }))
    renderShell('/settings', rpc)
    const active = await screen.findByTestId('settings-active')
    expect(active).toHaveTextContent('openrouter')
    expect(active).toHaveTextContent('openai/gpt-6-luna')
    const readiness = screen.getByTestId('settings-readiness')
    expect(readiness).toHaveTextContent('Restart needed')
    expect(readiness).toHaveTextContent('Changes paused')
    model = 'z-ai/glm-5.3'
    await user.click(screen.getByRole('button', { name: 'Reload' }))
    await waitFor(() => expect(active).toHaveTextContent('z-ai/glm-5.3'))
  })
})

describe('restart reasons', () => {
  it('reads as words, with an unknown id shown as it is', () => {
    expect(
      ['heartbeat_watcher', 'sandbox', 'gateway_bind', 'brand_new'].map(restartReasonLabel),
    ).toEqual(['heartbeat', 'sandbox', 'gateway address', 'brand_new'])
  })
})

describe('settings search', () => {
  it('finds a field and moves focus to it', async () => {
    const user = userEvent.setup()
    const { router } = await openAdvanced()
    await user.click(screen.getByRole('link', { name: 'Health' }))
    await user.type(screen.getByRole('searchbox', { name: 'Search settings' }), 'webhook')
    await user.click(
      await screen.findByRole('button', {
        name: 'Cron failure webhook (cron.failure_webhook) — Advanced',
      }),
    )
    expect(router.state.location.pathname).toBe('/settings/advanced')
    await waitFor(() => expect(screen.getByRole('textbox', { name: HOOK })).toHaveFocus())
  })

  it('repeats the query when nothing matches', async () => {
    const user = userEvent.setup()
    await openAdvanced()
    await user.type(screen.getByRole('searchbox', { name: 'Search settings' }), 'zzzqqq')
    expect(screen.getByText('No settings match “zzzqqq”')).toBeInTheDocument()
  })
})

describe('Advanced (schema form)', () => {
  it('shows an optional enum as Default plus exactly its values, with the description as help', async () => {
    await openAdvanced()
    const select = screen.getByRole('combobox', { name: 'mode' })
    expect(
      within(select)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Default', 'a', 'b', 'c'])
    expect(select).toHaveAccessibleDescription('How the gateway runs')
  })

  it('labels every row with its dotted path, the schema title as secondary text', async () => {
    await openAdvanced()
    expect(screen.getByRole('textbox', { name: HOOK })).toBeInTheDocument()
    // A title that only repeats the last path segment adds nothing.
    expect(screen.getByRole('switch', { name: 'agentos_router.enabled' })).toBeInTheDocument()
  })

  it('keeps a key the schema does not describe editable, typed from its value', async () => {
    await openAdvanced()
    const field = screen.getByRole('spinbutton', { name: 'custom_unknown' })
    expect(field).toHaveValue(7)
    expect(field).not.toBeDisabled()
  })
})

describe('save bar', () => {
  it('counts and reviews pending changes as path: old → new', async () => {
    const user = userEvent.setup()
    await openAdvanced()
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(screen.getByRole('switch', { name: 'agentos_router.enabled' }))
    expect(screen.getByTestId('unsaved-count')).toHaveTextContent('2 unsaved changes')
    await user.click(screen.getByRole('button', { name: 'Review changes' }))
    const review = await screen.findByTestId('pending-review')
    expect(review).toHaveTextContent('mode: "a" → "b"')
    expect(review).toHaveTextContent('agentos_router.enabled: true → false')
  })

  it('asks before leaving with unsaved edits; navigation waits for Discard', async () => {
    const user = userEvent.setup()
    const { router } = await openAdvanced()
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await act(() => router.navigate('/home'))
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave with unsaved changes?' })
    expect(router.state.location.pathname).toBe('/settings/advanced')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Keep editing' })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/home'))
  })

  it('refuses a save on a revision conflict, keeps the edits and offers Reload / Overwrite', async () => {
    const user = userEvent.setup()
    let calls = 0
    const rpc = settingsRpc((p) => {
      calls++
      if (calls === 1)
        throw new Error(
          'config revision mismatch: expected rev-1, current rev-2; refresh and retry',
        )
      return { ok: true, echo: p }
    })
    await openAdvanced(rpc)
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(
      await screen.findByText('The configuration changed since you opened it'),
    ).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'mode' })).toHaveValue('b')
    const conflict = screen.getByText('The configuration changed since you opened it')
      .parentElement!.parentElement as HTMLElement
    expect(within(conflict).getByRole('button', { name: 'Reload' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Overwrite' }))
    await waitFor(() => expect(rpc.callsTo('config.patch')).toHaveLength(2))
    expect(rpc.callsTo('config.patch')[1]).toMatchObject({
      patches: { mode: 'b' },
      expectedRevision: 'rev-1',
    })
  })

  it('shows a validation error at its field and keeps every edit', async () => {
    const user = userEvent.setup()
    const rpc = settingsRpc(() => {
      throw new Error(
        '1 validation error for GatewayConfig\ncron.failure_webhook\n  Value error, must be an https URL [type=value_error, input_value=x]',
      )
    })
    await openAdvanced(rpc)
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(screen.getByRole('switch', { name: 'agentos_router.enabled' }))
    const hook = screen.getByRole('textbox', { name: HOOK })
    await user.clear(hook)
    await user.type(hook, 'x')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Value error, must be an https URL')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: HOOK })).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByTestId('unsaved-count')).toHaveTextContent('3 unsaved changes')
  })
})

describe('restart-required changes', () => {
  // The gateway's real shapes: config.patch answers only `restartRequired`;
  // what needs the restart is reported by config.snapshot.
  function restartRpc(pendingAtStart = false) {
    let pending = pendingAtStart
    const rpc = settingsRpc(() => {
      pending = true
      return { patched: ['mode'], restartRequired: true }
    })
      .handle('config.snapshot', () => ({
        config: CONFIG,
        revision: 'rev-1',
        pendingRestart: pending,
        restartReasons: pending ? ['channels'] : [],
      }))
      .handle('gateway.restart', () => {
        pending = false
        return { ok: true }
      })
    return rpc
  }

  it('names the reason from the snapshot after a save, restarts the gateway and clears it', async () => {
    const user = userEvent.setup()
    const rpc = restartRpc()
    await openAdvanced(rpc)
    expect(screen.queryByText(/takes effect after/)).not.toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'c')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    const notice = await screen.findByText(/A saved change takes effect after a restart: channels/)
    // Docked after the section, in the save bar's place: inserting it above
    // the section pushed the page down under the reader.
    const dock = notice.closest('.set-restart-dock')!
    expect(dock).not.toBeNull()
    const pane = screen.getByRole('region', { name: /settings$/ })
    const header = within(pane).getByRole('heading', { level: 1 })
    expect(header.compareDocumentPosition(dock) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Restart gateway' }))
    await waitFor(() => expect(rpc.callsTo('gateway.restart')).toHaveLength(1))
    await waitFor(() =>
      expect(screen.queryByText(/takes effect after a restart/)).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows a restart another client caused, with no write from this page', async () => {
    await openAdvanced(restartRpc(true))
    expect(
      await screen.findByText(/A saved change takes effect after a restart: channels/),
    ).toBeInTheDocument()
  })

  it('Later hides the notice until another write needs a restart', async () => {
    const user = userEvent.setup()
    await openAdvanced(restartRpc(true))
    await user.click(await screen.findByRole('button', { name: 'Later' }))
    expect(screen.queryByText(/takes effect after a restart/)).not.toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(
      await screen.findByText(/A saved change takes effect after a restart: channels/),
    ).toBeInTheDocument()
  })

  it('falls back to the write result on a gateway whose snapshot does not report restarts', async () => {
    const user = userEvent.setup()
    const rpc = settingsRpc(() => ({ restartRequired: true }))
    await openAdvanced(rpc)
    await user.selectOptions(screen.getByRole('combobox', { name: 'mode' }), 'b')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(
      await screen.findByText('A saved change takes effect after the gateway restarts.'),
    ).toBeInTheDocument()
  })
})

describe('guided setup', () => {
  it('prompts on first run and opens the stepper at the provider step', async () => {
    const user = userEvent.setup()
    const rpc = settingsRpc()
      .handle('onboarding.status', { needsOnboarding: true, llmConfigured: false })
      .handle('onboarding.catalog', {})
      .handle('config.snapshot', {
        config: {},
        revision: 'r',
        status: { needsOnboarding: true },
        catalog: { providers: [{ providerId: 'openrouter', label: 'OpenRouter', fields: [] }] },
      })
    renderShell('/home', rpc)
    const prompt = await screen.findByText('Finish setting up AgentOS')
    await user.click(
      within(prompt.closest('[role="status"]') as HTMLElement).getByRole('link', {
        name: 'Open guided setup',
      }),
    )
    expect(
      await screen.findByRole('button', { name: /Provider/, current: 'step' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Provider' })).toHaveValue('openrouter')
  })

  it('shrinks to a short title and the action on phones, still dismissible', async () => {
    const user = userEvent.setup()
    renderShell(
      '/home',
      settingsRpc().handle('onboarding.status', { needsOnboarding: true, llmConfigured: false }),
    )
    const full = await screen.findByText('Finish setting up AgentOS')
    const prompt = full.closest('[role="status"]') as HTMLElement
    // At 360 px the title, body and action wrapped to seven lines (~190 px).
    expect(full.className).toMatch(/max-sm:hidden/)
    expect(within(prompt).getByText('Finish setup').className).toMatch(/(^|\s)sm:hidden/)
    expect(
      within(prompt).getByText('Choose a provider and router so the agent can answer.').className,
    ).toMatch(/max-sm:sr-only/)
    await user.click(within(prompt).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('Finish setting up AgentOS')).not.toBeInTheDocument()
  })
})

describe('appearance', () => {
  it('spaces each choice group below the divider (a legend ignores its fieldset padding)', async () => {
    renderShell('/settings/appearance', settingsRpc())
    for (const name of ['Theme', 'Text size', 'Send shortcut']) {
      const group = await screen.findByRole('group', { name })
      expect(group.className).not.toMatch(/(^|\s)py-3/)
      expect(group.parentElement?.className).toMatch(/(^|\s)py-3(\s|$)/)
    }
  })

  it('centres each segmented item in its touch target and marks the choice in forced colors', async () => {
    renderShell('/settings/appearance', settingsRpc())
    const group = await screen.findByRole('group', { name: 'Theme' })
    // `target` grows an item to 44px on touch screens; the text sat at its top.
    for (const radio of within(group).getAllByRole('radio')) {
      const item = radio.closest('label') as HTMLElement
      expect(item.className).toMatch(/(^|\s)target(\s|$)/)
      expect(item.className).toMatch(/(^|\s)inline-flex(\s|$)/)
      expect(item.className).toMatch(/(^|\s)items-center(\s|$)/)
    }
    const chosen = within(group)
      .getAllByRole('radio')
      .find((r) => (r as HTMLInputElement).checked)
    expect(chosen?.closest('label')?.className).toMatch(/forced-colors:bg-\[Highlight\]/)
  })
})
