import { t, type MessageKey } from '~/i18n'
import type { Tone } from '../parts'
import type { SetupItem, SetupSummary } from './setup-items'

/**
 * What the Overview's head pill and hero say, from one summary, so the two
 * can never disagree: the pill counts, the hero names. Neither says the rest
 * is fine while anything else needs fixing. Pure, for the tests.
 */

function fill(key: MessageKey, vars: Record<string, string | number>): string {
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replace(`{${name}}`, String(value)),
    t(key),
  )
}

/** "Search on X" → "search on X" for the middle of a sentence. */
export function midSentence(title: string): string {
  return title.charAt(0).toLowerCase() + title.slice(1)
}

export function pillCopy(summary: SetupSummary): { text: string; tone: Tone } {
  if (summary.phase === 'offline') {
    return { text: t('settings.overview.pill.offline'), tone: 'danger' }
  }
  if (summary.requiredLeft > 0) {
    return {
      text:
        summary.requiredLeft === 1
          ? t('settings.overview.pill.requiredOne')
          : fill('settings.overview.pill.requiredMany', { n: summary.requiredLeft }),
      tone: 'warn',
    }
  }
  if (summary.toFix > 0) {
    return { text: fill('settings.overview.pill.toFix', { n: summary.toFix }), tone: 'warn' }
  }
  if (summary.optional > 0) {
    return { text: fill('settings.overview.pill.optional', { n: summary.optional }), tone: 'ok' }
  }
  return { text: t('settings.overview.pill.allSet'), tone: 'ok' }
}

export interface HeroCopy {
  title: string
  body: string
}

/** The hero for a reachable gateway (the offline hero depends on the process, not the summary). */
export function heroCopy(summary: SetupSummary): HeroCopy {
  const next = summary.next
  if (summary.phase === 'blocked' && next) {
    const more = summary.requiredOpen.length - 1 + summary.broken.length
    return {
      title: t('settings.overview.hero.blocked.title'),
      body:
        more === 0
          ? fill('settings.overview.hero.blocked.body', { detail: next.detail })
          : more === 1
            ? fill('settings.overview.hero.blocked.oneMore', { detail: next.detail })
            : fill('settings.overview.hero.blocked.more', { detail: next.detail, n: more }),
    }
  }
  if (summary.phase === 'attention' && next) {
    return attentionCopy(summary.broken)
  }
  if (summary.phase === 'optional' && next) {
    return {
      title: t('settings.overview.hero.optional.title'),
      body: fill('settings.overview.hero.optional.body', {
        suggest: t(`settings.overview.suggest.${next.id}` as MessageKey),
      }),
    }
  }
  return {
    title: t('settings.overview.hero.complete.title'),
    body: t('settings.overview.hero.complete.body'),
  }
}

/** One broken item is named with its fix; several are counted, the first leads. */
function attentionCopy(broken: readonly SetupItem[]): HeroCopy {
  const [first, second, third] = broken
  if (!first) return { title: '', body: '' }
  if (!second) {
    return {
      title: fill('settings.overview.hero.attention.title', { title: first.title }),
      body: fill('settings.overview.hero.attention.body', { detail: first.detail }),
    }
  }
  const lead = { title: midSentence(first.title), detail: first.detail }
  const title = fill('settings.overview.hero.attention.many', { n: broken.length })
  if (broken.length === 2) {
    return {
      title,
      body: fill('settings.overview.hero.attention.two', {
        ...lead,
        second: midSentence(second.title),
      }),
    }
  }
  if (broken.length === 3 && third) {
    return {
      title,
      body: fill('settings.overview.hero.attention.three', {
        ...lead,
        second: midSentence(second.title),
        third: midSentence(third.title),
      }),
    }
  }
  return {
    title,
    body: fill('settings.overview.hero.attention.more', { ...lead, n: broken.length - 1 }),
  }
}
