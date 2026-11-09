// Event-stream fixtures for the transcript model, one or more per
// session.event.* type, each with the history page the gateway serves for the
// same turn afterwards (when the turn persists). Shapes follow the wire
// protocol: engine/types.py events as published by gateway/rpc_sessions.py,
// and chat/history.py rows. Hand-written from those sources.
import type { HistoryPage } from '../types'
import { CANCELLED_TOOL_RESULT, TURN_INTERRUPTED_NOTE } from '../reducer'

export interface StreamFixture {
  name: string
  /** The user's message, sent from this tab (optimisticSend) — or null. */
  send: string | null
  events: { name: string; payload: Record<string, unknown> }[]
  /** The history page after the turn; absent when nothing persists. */
  history?: HistoryPage
}

const KEY = 'agent:main:webchat:fx'
const ev = (kind: string, payload: Record<string, unknown> = {}) => ({
  name: `session.event.${kind}`,
  payload: { session_key: KEY, epoch: 0, ...payload },
})
const user = (id: string, text: string, ts = 1_700_000_000) => ({
  id,
  message_id: id,
  role: 'user',
  text,
  timestamp: ts,
})

const USAGE = {
  input_tokens: 1200,
  output_tokens: 340,
  cost_usd: 0.0042,
  model: 'anthropic/claude-sonnet',
  routed_model: 'anthropic/claude-sonnet',
  routed_tier: 'c2',
  routing_source: 'pilot',
}
const DONE_USAGE = { ...USAGE }

// The artifact payload carries its session (gateway artifact store record).
const chartArtifact = {
  session_key: KEY,
  id: 'art-chart-1',
  name: 'btc.chart.json',
  mime: 'application/vnd.agentos.chart+json',
  size: 2048,
  download_url: '/api/v1/artifacts/art-chart-1',
}

export const STREAMS: StreamFixture[] = [
  {
    name: 'text_delta + done: a plain answer',
    send: 'hello',
    events: [
      ev('state_change', { from_state: 'idle', to_state: 'thinking' }),
      ev('text_delta', { text: 'Hi' }),
      ev('text_delta', { text: ' there' }),
      ev('text_delta', { text: '!' }),
      ev('done', {
        text: 'Hi there!',
        outcome: { kind: 'completed' },
        state: 'done',
        ...DONE_USAGE,
      }),
    ],
    history: {
      messages: [
        user('u1', 'hello'),
        {
          id: 'a1',
          message_id: 'a1',
          role: 'assistant',
          text: 'Hi there!',
          timestamp: 1_700_000_005,
          usage: USAGE,
        },
      ],
      has_more: false,
    },
  },
  {
    name: 'router_decision, thinking, two tools, a chart artifact',
    send: 'chart btc',
    events: [
      ev('router_decision', { tier: 'c2', model: 'anthropic/claude-sonnet', source: 'pilot' }),
      ev('thinking', { text: 'Need prices. ' }),
      ev('thinking', { text: 'Then chart.' }),
      ev('tool_use_start', { tool_use_id: 't1', tool_name: 'web_search' }),
      ev('tool_result', {
        tool_use_id: 't1',
        tool_name: 'web_search',
        result: '[{"title":"BTC"}]',
        is_error: false,
        arguments: { query: 'btc price' },
      }),
      ev('tool_use_start', { tool_use_id: 't2', tool_name: 'bash' }),
      ev('run_heartbeat', { phase: 'tool', elapsed_ms: 5000, idle_ms: 100 }),
      ev('tool_result', {
        tool_use_id: 't2',
        tool_name: 'bash',
        result: 'ok',
        is_error: false,
        arguments: { command: 'python chart.py' },
        execution_status: { status: 'ok', exit_code: 0, truncated: false },
      }),
      ev('artifact', chartArtifact),
      ev('text_delta', { text: 'Here is the chart.' }),
      ev('done', {
        text: 'Here is the chart.',
        outcome: { kind: 'completed' },
        state: 'done',
        ...DONE_USAGE,
      }),
    ],
    history: {
      messages: [
        user('u2', 'chart btc'),
        {
          id: 'a2',
          message_id: 'a2',
          role: 'assistant',
          text: 'Here is the chart.',
          timestamp: 1_700_000_042,
          has_thinking: true,
          usage: USAGE,
          artifacts: [chartArtifact],
          tool_calls: [
            {
              type: 'tool_use',
              tool_use_id: 't1',
              name: 'web_search',
              input: { query: 'btc price' },
            },
            {
              type: 'tool_result',
              tool_use_id: 't1',
              name: 'web_search',
              result: '[{"title":"BTC"}]',
              is_error: false,
            },
            {
              type: 'tool_use',
              tool_use_id: 't2',
              name: 'bash',
              input: { command: 'python chart.py' },
            },
            {
              type: 'tool_result',
              tool_use_id: 't2',
              name: 'bash',
              result: 'ok',
              is_error: false,
              execution_status: { status: 'ok', exit_code: 0, truncated: false },
            },
            { type: 'text', text: 'Here is the chart.' },
          ],
        },
      ],
    },
  },
  {
    name: 'done (aborted): a stopped turn with a running tool',
    send: 'long job',
    events: [
      ev('text_delta', { text: 'Starting the job' }),
      ev('tool_use_start', { tool_use_id: 't3', tool_name: 'bash' }),
      ev('done', {
        state: 'aborted',
        reason: 'aborted',
        partial_text: 'Starting the job',
        outcome: { kind: 'interrupted', reason: 'aborted' },
      }),
    ],
    history: {
      messages: [
        user('u3', 'long job'),
        {
          id: 'a3',
          message_id: 'a3',
          role: 'assistant',
          text: `Starting the job\n\n${TURN_INTERRUPTED_NOTE}`,
          timestamp: 1_700_000_010,
          tool_calls: [
            { type: 'text', text: 'Starting the job' },
            { type: 'tool_use', tool_use_id: 't3', name: 'bash', input: { command: 'sleep 999' } },
            {
              type: 'tool_result',
              tool_use_id: 't3',
              name: 'bash',
              result: CANCELLED_TOOL_RESULT,
              is_error: true,
            },
            { type: 'text', text: `\n\n${TURN_INTERRUPTED_NOTE}` },
          ],
        },
      ],
    },
  },
  {
    name: 'error: a provider failure',
    send: 'explain',
    events: [
      ev('text_delta', { text: '' }),
      ev('error', { message: 'Provider overloaded', code: 'provider_error' }),
    ],
    history: {
      messages: [
        user('u4', 'explain'),
        {
          id: 'e4',
          message_id: 'e4',
          role: 'error',
          text: 'Provider overloaded',
          timestamp: 1_700_000_003,
        },
      ],
    },
  },
  {
    name: 'tool_result: exit_plan_mode presents a plan',
    send: 'plan it',
    events: [
      ev('tool_use_start', { tool_use_id: 'p1', tool_name: 'exit_plan_mode' }),
      ev('tool_result', {
        tool_use_id: 'p1',
        tool_name: 'exit_plan_mode',
        result: JSON.stringify({ status: 'presented', plan: '1. Read\n2. Write' }),
        is_error: false,
        arguments: {},
      }),
      ev('done', { text: '', outcome: { kind: 'completed' }, state: 'done' }),
    ],
    history: {
      messages: [
        user('u5', 'plan it'),
        {
          id: 'a5',
          message_id: 'a5',
          role: 'assistant',
          text: '',
          timestamp: 1_700_000_004,
          tool_calls: [
            { type: 'tool_use', tool_use_id: 'p1', name: 'exit_plan_mode', input: {} },
            {
              type: 'tool_result',
              tool_use_id: 'p1',
              name: 'exit_plan_mode',
              result: JSON.stringify({ status: 'presented', plan: '1. Read\n2. Write' }),
              is_error: false,
            },
          ],
        },
      ],
    },
  },
  {
    name: 'tool_result: ask_user asks a question',
    send: 'book it',
    events: [
      ev('tool_use_start', { tool_use_id: 'q1', tool_name: 'ask_user' }),
      ev('tool_result', {
        tool_use_id: 'q1',
        tool_name: 'ask_user',
        result: JSON.stringify({
          questions: [{ question: 'Which day?', options: ['Mon', 'Tue'] }],
        }),
        is_error: false,
        arguments: {},
      }),
      ev('done', { text: '', outcome: { kind: 'completed' }, state: 'done' }),
    ],
  },
  {
    name: 'steering: a mid-turn message',
    send: 'research',
    events: [
      ev('text_delta', { text: 'Looking' }),
      ev('steering', { text: 'focus on ETH', message_id: 'm-s', task_id: 'x' }),
      ev('text_delta', { text: ' at ETH' }),
      ev('done', { text: 'Looking at ETH', outcome: { kind: 'completed' }, state: 'done' }),
    ],
  },
  {
    name: 'subagent_completion inside a turn',
    send: 'delegate',
    events: [
      ev('text_delta', { text: 'Delegated.' }),
      ev('subagent_completion', {
        type: 'subagent_completion',
        child_session_key: 'agent:main:subagent:c1',
        status: 'succeeded',
        result: 'child done',
      }),
      ev('done', { text: 'Delegated.', outcome: { kind: 'completed' }, state: 'done' }),
    ],
  },
  {
    name: 'cron_result: an automation reply',
    send: null,
    events: [
      ev('cron_result', {
        sessionKey: KEY,
        message: { role: 'assistant', text: 'Daily report ready', provenanceKind: 'cron' },
      }),
    ],
    history: {
      messages: [
        {
          id: 'c1',
          message_id: 'c1',
          role: 'assistant',
          text: 'Daily report ready',
          timestamp: 1_700_000_100,
          provenance_kind: 'cron',
        },
      ],
    },
  },
  {
    name: 'compaction (completed): a divider',
    send: null,
    events: [
      ev('compaction', { status: 'started' }),
      ev('compaction', {
        status: 'completed',
        summary: 'Earlier talk about BTC',
        removed_count: 12,
      }),
    ],
    history: {
      messages: [],
      compaction_summaries: [
        {
          id: 'cs1',
          summary_text: 'Earlier talk about BTC',
          removed_count: 12,
          created_at: 1_700_000_200,
        },
      ],
    },
  },
  {
    name: 'warning: transient, never part of a turn',
    send: 'warn me',
    events: [
      ev('warning', { code: 'slow', message: 'The provider is slow' }),
      ev('text_delta', { text: 'ok' }),
      ev('done', { text: 'ok', outcome: { kind: 'completed' }, state: 'done' }),
    ],
    history: {
      messages: [
        user('u6', 'warn me'),
        { id: 'a6', message_id: 'a6', role: 'assistant', text: 'ok', timestamp: 1_700_000_002 },
      ],
    },
  },
  {
    name: 'task_group: subagents keep the session running',
    send: null,
    events: [
      {
        name: 'session.event.task_group.waiting',
        payload: { group_id: 'subagent:k:t', pending_count: 2 },
      },
      { name: 'session.event.task_group.done', payload: { group_id: 'subagent:k:t' } },
    ],
  },
]

export { KEY as FIXTURE_SESSION_KEY }
