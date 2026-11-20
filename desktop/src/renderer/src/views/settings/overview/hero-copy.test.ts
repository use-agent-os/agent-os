import { describe, expect, it } from 'vitest'
import { heroCopy, pillCopy } from './hero-copy'
import { summarizeSetup, type SetupItem, type SetupItemId } from './setup-items'

function row(
  id: SetupItemId,
  group: SetupItem['group'],
  state: SetupItem['state'],
  title: string,
  detail: string,
): SetupItem {
  return {
    id,
    group,
    state,
    title,
    detail,
    action: '',
    jump: { kind: 'section', section: 'overview' },
  }
}

const GATEWAY = row('gateway', 'required', 'done', 'Gateway running', 'Running')
const PROVIDER_OK = row('provider', 'required', 'done', 'Provider and model', 'Surplus · luna')
const PROVIDER_MISSING = row(
  'provider',
  'required',
  'todo',
  'Provider and model',
  'No provider yet',
)
const BROKEN = [
  row('voice', 'recommended', 'attention', 'Voice', 'ElevenLabs · ELEVENLABS_API_KEY is not set'),
  row('x', 'recommended', 'attention', 'Search on X', 'On, but XAI_API_KEY is not set'),
  row('memory', 'recommended', 'attention', 'Memory', 'OpenAI · OPENAI_API_KEY is not set'),
]
const OPTIONAL = row('channels', 'recommended', 'todo', 'Channels', 'No channels yet')

function scenario(requiredDone: boolean, problems: number) {
  const items = [
    GATEWAY,
    requiredDone ? PROVIDER_OK : PROVIDER_MISSING,
    ...BROKEN.slice(0, problems),
    OPTIONAL,
  ]
  const summary = summarizeSetup(items)
  return { summary, pill: pillCopy(summary).text, hero: heroCopy(summary) }
}

/** What would tell a person "nothing else is wrong". */
const ALL_FINE = /fine|in place|nothing else needs/i

describe('Overview pill and hero', () => {
  it.each([
    // required done
    [true, 0, 'Required setup done · 1 optional', 'Your agent is ready'],
    [true, 1, 'Required setup done · 1 to fix', 'Voice needs attention'],
    [true, 2, 'Required setup done · 2 to fix', '2 things need attention'],
    [true, 3, 'Required setup done · 3 to fix', '3 things need attention'],
    // a required step open
    [false, 0, '1 required step left', 'Your agent cannot answer yet'],
    [false, 1, '1 required step left', 'Your agent cannot answer yet'],
    [false, 2, '1 required step left', 'Your agent cannot answer yet'],
    [false, 3, '1 required step left', 'Your agent cannot answer yet'],
  ] as const)('required done: %s, %i problems → "%s" / "%s"', (done, problems, pill, title) => {
    const { hero, pill: text } = scenario(done, problems)
    expect(text).toBe(pill)
    expect(hero.title).toBe(title)
  })

  it('one problem: named with its fix, and only then is the rest called fine', () => {
    const { hero } = scenario(true, 1)
    expect(hero.body).toBe('ElevenLabs · ELEVENLABS_API_KEY is not set. Nothing else needs fixing.')
  })

  it('several problems: counted, the first leads, the others are named or counted', () => {
    expect(scenario(true, 2).hero.body).toBe(
      'Start with voice: ElevenLabs · ELEVENLABS_API_KEY is not set. Then search on X.',
    )
    expect(scenario(true, 3).hero.body).toBe(
      'Start with voice: ElevenLabs · ELEVENLABS_API_KEY is not set. Then search on X and memory.',
    )
    for (const problems of [2, 3]) {
      expect(scenario(true, problems).hero.body).not.toMatch(ALL_FINE)
    }
  })

  it('more than three: the rest is counted, not listed', () => {
    const items = [
      GATEWAY,
      PROVIDER_OK,
      ...BROKEN,
      row('image', 'recommended', 'attention', 'Image generation', 'OpenAI · no key'),
    ]
    const summary = summarizeSetup(items)
    expect(heroCopy(summary)).toEqual({
      title: '4 things need attention',
      body: 'Start with voice: ElevenLabs · ELEVENLABS_API_KEY is not set. Then 3 more, marked below.',
    })
    expect(pillCopy(summary).text).toBe('Required setup done · 4 to fix')
  })

  it('a required step open says how much waits after it, never that the rest is fine', () => {
    expect(scenario(false, 0).hero.body).toBe('No provider yet. Everything else can wait.')
    expect(scenario(false, 1).hero.body).toBe(
      'No provider yet. After that, 1 more thing needs attention.',
    )
    expect(scenario(false, 3).hero.body).toBe(
      'No provider yet. After that, 3 more things need attention.',
    )
    for (const problems of [0, 1, 2, 3]) {
      expect(scenario(false, problems).hero.body).not.toMatch(ALL_FINE)
    }
  })

  it('nothing left at all is the only "All set"', () => {
    const summary = summarizeSetup([GATEWAY, PROVIDER_OK])
    expect(pillCopy(summary)).toEqual({ text: 'All set', tone: 'ok' })
    expect(heroCopy(summary).title).toBe('Everything is set up')
  })
})
