import { defineNamespace } from '../registry'

/**
 * The app shell: navigation, connection footer, route errors, the global
 * approval prompt, and the shared chrome components. View bodies live in their
 * own namespaces.
 */
export const shell = defineNamespace('shell', {
  // View titles. One definition, read by both the router and the sidebar.
  viewOverview: 'Overview',

  // Sidebar groups.
  navApprovalBadge_one: '{count} pending approval',
  navApprovalBadge_other: '{count} pending approvals',

  // Connection footer (the truth badge's tooltip).
  connecting: 'Connecting…',

  // Sidebar utilities.
  shortcutsLabel: 'Keyboard shortcuts',
  shortcutClose: 'Close dialog',
  shortcutSeparator: 'then',
  shortcutCategoryChat: 'Chat',
  shortcutNewChat: 'Start a new chat',
  shortcutAbortTurn: 'Abort the streaming turn, else recover the queue into the composer',

  // Route error boundary.
  errorHttpCode: 'HTTP {status}',

  // Truth badge: the footer's runtime status (state, source + freshness, and
  // one recovery action).
  truthReady: 'Ready',
  truthDegraded: 'Degraded',
  truthConnecting: 'Connecting',
  truthBlocked: 'Blocked',
  truthOffline: 'Offline',
  truthUnknown: 'Unknown',
  truthLive: 'Live · {age}',
  truthSnapshot: 'Snapshot · {age}',
  truthSnapshotAgeless: 'Snapshot',
  truthUnavailable: 'Unavailable',
  truthReasonOk: 'Connected, and the doctor reports no errors.',
  truthReasonConnecting: 'Opening the gateway connection.',
  truthReasonGatewayStarting: 'The gateway process is starting.',
  truthReasonGatewayStopped: 'The gateway process is not running.',
  truthReasonDisconnected: 'Lost the gateway connection. What you see is the last snapshot.',
  truthReasonNeverConnected: 'The gateway has not answered yet.',
  truthReasonAuthFailed: 'The gateway refused this console’s credentials.',
  truthReasonAuthRateLimited:
    'Too many failed sign-in attempts; the gateway is refusing this console for now.',
  truthReasonRestarting: 'The gateway is restarting and said it would be back shortly.',
  truthReasonProtocolMismatch: 'The gateway speaks a different protocol version than this console.',
  truthReasonHandshakeRefused: 'The gateway refused the connection.',
  truthReasonStale: 'No frame from the gateway for {age}. The view may be stale.',
  truthReasonDoctorErrors_one: '{count} doctor finding is at error level.',
  truthReasonDoctorErrors_other: '{count} doctor findings are at error level.',
  truthReasonSafeMode: 'The gateway is running in safe mode.',
  truthReasonDraining: 'The gateway is draining before a restart.',
  truthReasonStarting: 'The gateway is still starting up.',
  truthReasonChecking: 'Connected. Waiting for the first doctor report.',
  truthReasonDoctorUnavailable: 'Connected, but the doctor report could not be loaded.',
  truthActionRetry: 'Retry connection',
  truthActionStart: 'Start gateway',
  truthActionInbox: 'Open attention inbox',
  truthActionHealth: 'Open Health',
  ageSeconds: '{seconds} s',
  ageMinutes: '{minutes} min',
  ageHours: '{hours} h',

  // Typed runtime issues (attention inbox, desktop attention panel).
  issueGatewayUnreachableTitle: 'Gateway unreachable',
  issueGatewayUnreachableDetail:
    'The console lost its connection to the gateway. Live updates and actions are paused until it reconnects.',
  issueAuthFailedTitle: 'Gateway refused the credentials',
  issueAuthFailedDetail:
    'The handshake was refused ({message}). Check the gateway token, then retry.',
  issueProtocolMismatchTitle: 'Console and gateway protocol differ',
  issueProtocolMismatchReload:
    'This console is older than the gateway. Reload the page to load the matching one.',
  issueAuthRateLimitedTitle: 'Gateway is refusing sign-in attempts',
  issueAuthRateLimitedDetail:
    'Too many failed attempts with a wrong token. The gateway accepts another in {age}; check the token first.',
  issueAuthRateLimitedDetailBare:
    'Too many failed attempts with a wrong token. Check the token, then retry in a while.',
  issueProtocolMismatchDetail:
    'The gateway answered “{message}”. Upgrade whichever side is older, then retry.',
  issueSafeModeTitle: 'Gateway in safe mode',
  issueSafeModeDetail: 'The gateway started in safe mode. Failing: {failing}.',
  issueSafeModeDetailBare: 'The gateway started in safe mode; some subsystems are off.',
  issueUpdateFailedTitle: 'Update failed',
  issueFindingUntitled: 'Doctor finding {id}',
  issueActionRestart: 'Restart gateway',
  issueActionRetry: 'Retry connection',
  issueActionFixSteps: 'Open doctor fix steps',
  issueActionSettings: 'Open settings',
  issueActionDismiss: 'Dismiss',
  issueRestartConfirm:
    'Restart the gateway? Running turns are interrupted and pick up again once it is back.',

  // Disabled-with-reason: why a visible control cannot be used right now.
  reasonGatewayOffline: 'The gateway is offline. Reconnect to use this.',
  reasonGatewayConnecting: 'Waiting for the gateway connection.',
  reasonSchedulerUnavailable: 'The scheduler is not running on this gateway.',
  reasonApprovalsPending_one: '{count} approval is waiting. Answer it before updating.',
  reasonApprovalsPending_other: '{count} approvals are waiting. Answer them before updating.',
  reasonPreflightChecking: 'Checking what an update would interrupt.',

  // Update preflight.
  preflightSessions_one: '{count} session is running',
  preflightSessions_other: '{count} sessions are running',
  preflightJobs_one: '{count} scheduled job is due in the next 10 minutes',
  preflightJobs_other: '{count} scheduled jobs are due in the next 10 minutes',
  preflightApprovals_one: '{count} approval is pending',
  preflightApprovals_other: '{count} approvals are pending',
} as const)
