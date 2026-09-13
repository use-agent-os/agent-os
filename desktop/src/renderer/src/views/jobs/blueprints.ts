import type { RawJob } from '@/views/cron/logic'

/**
 * A ready-made job: one click opens the create sheet with this filled in.
 * `expression` is the cron cadence the builder will recognise; `hint` is the
 * one-line description shown under the name. Prompts are written for a
 * general-purpose assistant and can be edited before saving.
 */
export interface Blueprint extends Partial<RawJob> {
  id: string
  name: string
  hint: string
  expression: string
  payloadKind: 'reminder' | 'agent_turn'
  message: string
}

export const BLUEPRINTS: readonly Blueprint[] = [
  {
    id: 'morning-briefing',
    name: 'Morning briefing',
    hint: 'Weekdays at 08:00',
    expression: '0 8 * * 1-5',
    payloadKind: 'agent_turn',
    message:
      "Produce a concise morning briefing: today's calendar events, the local weather, and any urgent items. Keep it short and scannable. If no data sources are connected, give a brief good-morning with the date and offer to connect calendar or email.",
  },
  {
    id: 'important-mail',
    name: 'Important-mail monitor',
    hint: 'Every 30 minutes, working hours',
    expression: '*/30 8-18 * * 1-5',
    payloadKind: 'agent_turn',
    message:
      'Check the inbox for messages since the last run that need a reply or a decision today. Report only those, one line each with sender and what is being asked. Say "nothing urgent" when there is nothing.',
  },
  {
    id: 'weekly-review',
    name: 'Weekly review',
    hint: 'Fridays at 16:00',
    expression: '0 16 * * 5',
    payloadKind: 'agent_turn',
    message:
      "Summarize this week's work from my sessions and notes: what shipped, what slipped, and what is still open. Propose three priorities for next week.",
  },
  {
    id: 'workday-start',
    name: 'Workday start reminder',
    hint: 'Weekdays at 09:00, no model call',
    expression: '0 9 * * 1-5',
    payloadKind: 'reminder',
    message: 'Workday starting. Pick the one thing that matters most today before opening mail.',
  },
  {
    id: 'custom-reminder',
    name: 'Custom reminder',
    hint: 'Every day at 12:00, edit the text',
    expression: '0 12 * * *',
    payloadKind: 'reminder',
    message: 'Reminder text goes here.',
  },
  {
    id: 'evening-wind-down',
    name: 'Evening wind-down',
    hint: 'Every day at 21:00',
    expression: '0 21 * * *',
    payloadKind: 'agent_turn',
    message:
      'Write a short end-of-day note: what got done today, anything left unfinished worth carrying to tomorrow, and one line to close the day. Warm, brief, no bullet overload.',
  },
  {
    id: 'topic-digest',
    name: 'Topic news digest',
    hint: 'Every day at 07:30',
    expression: '30 7 * * *',
    payloadKind: 'agent_turn',
    message:
      'Search for the most significant news from the last 24 hours on: <topic>. Return the five most important items with a one-sentence why-it-matters each and a source link.',
  },
  {
    id: 'bills-renewals',
    name: 'Bills & renewals reminder',
    hint: 'The 1st of every month at 09:00',
    expression: '0 9 1 * *',
    payloadKind: 'reminder',
    message:
      'New month: review subscriptions, bills due, and anything renewing in the next 30 days.',
  },
  {
    id: 'price-watch',
    name: 'Price & availability watch',
    hint: 'Every 6 hours',
    expression: '0 */6 * * *',
    payloadKind: 'agent_turn',
    message:
      'Check the current price and availability of: <item or URL>. Report only if the price changed or the item came back in stock since the last run; otherwise reply "no change".',
  },
  {
    id: 'competitor-watch',
    name: 'Competitor news watch',
    hint: 'Mondays at 08:00',
    expression: '0 8 * * 1',
    payloadKind: 'agent_turn',
    message:
      'Look for news, launches, pricing changes, and notable hires at: <competitors>. Summarize in under 200 words with links. Skip anything already covered last week.',
  },
  {
    id: 'habit-checkin',
    name: 'Habit check-in',
    hint: 'Every day at 20:00, no model call',
    expression: '0 20 * * *',
    payloadKind: 'reminder',
    message: 'Habit check-in: did you do the thing today? Log it.',
  },
  {
    id: 'hydration',
    name: 'Hydration & movement nudge',
    hint: 'Every 2 hours, working hours',
    expression: '0 9-17/2 * * 1-5',
    payloadKind: 'reminder',
    message: 'Stand up, stretch for a minute, drink some water.',
  },
  {
    id: 'meal-plan',
    name: 'Weekly meal plan',
    hint: 'Sundays at 10:00',
    expression: '0 10 * * 0',
    payloadKind: 'agent_turn',
    message:
      'Draft a simple meal plan for the coming week: seven dinners, varied, quick on weekdays. Include a consolidated shopping list grouped by aisle.',
  },
  {
    id: 'learning-drip',
    name: 'Daily learning drip',
    hint: 'Every day at 13:00',
    expression: '0 13 * * *',
    payloadKind: 'agent_turn',
    message:
      'Teach me one concept about <subject> in under 150 words, with a concrete example and one question to test myself. Do not repeat a concept from the last two weeks.',
  },
  {
    id: 'gratitude',
    name: 'Gratitude & reflection prompt',
    hint: 'Every day at 21:30',
    expression: '30 21 * * *',
    payloadKind: 'agent_turn',
    message:
      'Offer one thoughtful reflection question for the end of the day and a short, specific gratitude prompt. Vary them; keep the whole thing under 60 words.',
  },
  {
    id: 'on-this-day',
    name: 'On-this-day discovery',
    hint: 'Every day at 08:15',
    expression: '15 8 * * *',
    payloadKind: 'agent_turn',
    message:
      'Share one interesting thing that happened on this date in history, in three or four sentences, with why it still matters. Pick something that is not the obvious headline.',
  },
]
