import { defineNamespace } from '../registry'

export const cron = defineNamespace('cron', {
  // Job card.
  concurrencyForbid: 'Forbid: skip a run while one is going',
  concurrencyReplace: 'Replace: stop the running one, start anew',
  concurrencyAllow: 'Allow: run side by side',

  // Create / edit panel.
  messageLabelEvent: 'Event text',
  messageLabelReminder: 'Reminder text',
  messageLabelNote: 'Note (not sent)',
  messageLabelTask: 'Task prompt',

  // Job kinds (logic.ts).
  kindReminder: 'Reminder',
  kindSystemEvent: 'System event',
  kindScript: 'Script',
  kindAgentTask: 'Agent task',

  // Next-run text (logic.ts).
  nextRunning: 'running',
  nextAwaiting: 'awaiting update',

  // Duration + countdown (logic.ts).
  durationSeconds: '{seconds}s',
  durationMinutes: '{minutes}m {seconds}s',
  durationHours: '{hours}h {minutes}m',
  durationDays: '{days}d {hours}h',
  countdownNow: 'now',
  countdownIn: 'in {duration}',
  countdownAgo: '{duration} ago',
  countdownJustNow: 'just now',
  timeToday: 'today {time}',
  timeTomorrow: 'tomorrow {time}',

  // Cron humanizer (logic.ts).
  dowSun: 'Sun',
  dowMon: 'Mon',
  dowTue: 'Tue',
  dowWed: 'Wed',
  dowThu: 'Thu',
  dowFri: 'Fri',
  dowSat: 'Sat',
  monJan: 'Jan',
  monFeb: 'Feb',
  monMar: 'Mar',
  monApr: 'Apr',
  monMay: 'May',
  monJun: 'Jun',
  monJul: 'Jul',
  monAug: 'Aug',
  monSep: 'Sep',
  monOct: 'Oct',
  monNov: 'Nov',
  monDec: 'Dec',
  explainEveryMinute: 'Every minute',
  explainEveryHourAt: 'Every hour at :{minute}',
  explainEveryDayAt: 'Every day at {time}',
  explainWeekdaysAt: 'Weekdays at {time}',
  explainWeekendsAt: 'Weekends at {time}',
  explainDaysAt: '{days} at {time}',
  explainDayOfMonthAt: 'Day {days} of every month at {time}',
  explainMonthsDaysAt: '{months} {days} at {time}',
  explainEveryNMinutes: 'Every {count} minutes',
  explainFallback: 'at minute {minute}, hour {hour}',
  explainAllMinutes: 'every minute',
  explainAllHours: 'every hour',
  explainMore: '{shown} & {count} more',

  // Save validation (logic.ts).
  errFailureWebhookUrl: 'Failure-destination webhook URL is required',
  errFailureChannel: 'Failure-destination channel is required',
  errWebhookUrl: 'Webhook URL is required for webhook delivery',
  errName: 'Name is required',
  errInterval: 'Interval must be an integer number of seconds',
  errElevatedNeedsTurn:
    'Elevated tools require an agent turn — reminder and system-event jobs never run one',
  errCurrentSessionKey: 'Current session key is required',
  errNamedSessionKey: 'Named session key is required',

  // Script-path validation (logic.ts).
  scriptRequired: 'Script path is required',
  scriptMustBeRelative:
    'Script path must be relative to ~/.agentos/scripts/ — use just the file name',
  scriptMustStayInside: 'Script path must stay inside ~/.agentos/scripts/',
} as const)
