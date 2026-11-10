import { defineNamespace } from '../registry'

/** Copy of the rebuilt web console's gateway connection surfaces. */
export const webConnection = defineNamespace('webConnection', {
  connectTitle: 'Connect to your gateway',
  connectBody: 'This gateway needs a token before the console can open.',
  connectBodyConnecting: 'The console is connecting to the gateway below.',
  connectWhere:
    'The token is the gateway’s auth.token setting (or AGENTOS_GATEWAY_TOKEN). Run “agentos config get auth.token” on the gateway host to read it.',
  connectAddress: 'Gateway: {address}',
  connectToken: 'Gateway token',
  connectUrl: 'WebSocket URL',
  connectUrlHelp: 'Change this only when the console should reach a different gateway.',
  connectSubmit: 'Connect',
  connectReaching: 'Connecting to {host}…',
  connectUnreachable:
    'Could not reach {host}. Check the WebSocket URL and that the gateway is running, then connect again.',
  connectTokenOptional: 'Leave empty if this gateway does not use a token.',
  connectRejected: 'The gateway rejected this token.',
  connectRateLimited: 'Sign-in is rate limited. Wait a moment, then reload the page to try again.',
  connectTokenStored: 'Stored for this tab only.',

  gatewayUrlTitle: 'Connect to a different gateway?',
  gatewayUrlBody: 'This link asks the console to connect to the gateway at {host}:',
  gatewayUrlNoToken:
    'Your current token is not sent there. If that gateway needs a token, the console asks you for it, and you will need your current token again to come back.',
  gatewayUrlLinkToken:
    'The token in the link is sent to {host}. Your current token is not sent there, and you will need it again to come back.',
  gatewayUrlSaved:
    '{host} becomes this browser’s gateway only after it connects. Only continue if you trust it.',
  gatewayUrlConfirm: 'Connect to {host}',
  gatewayUrlCancel: 'Stay here',

  restarting: 'Restarting gateway…',
  restartingBody: 'The console reconnects as soon as it is back.',
  protocolTitle: 'A newer console is available',
  protocolBody: 'The gateway was upgraded. Reload to load the matching console.',
  protocolReload: 'Reload',

  updateAvailable: 'Update available',
  updateNotice: 'AgentOS {version} is available.',
  updateNoticeLink: 'See Settings › Updates',
  updateDismiss: 'Dismiss',

  updateNow: 'Update now',
  updateConfirmTitle: 'Update to {version}?',
  updateConfirmBody: 'The gateway restarts to finish the update.',
  updateChecking: 'Checking what an update would interrupt…',
  updateCheckFailed: "Couldn't check running work: {message}",
  updateRunningSessions_one: '{count} running session will be interrupted:',
  updateRunningSessions_other: '{count} running sessions will be interrupted:',
  updateDueJobs_one: '{count} automation is due within 10 minutes:',
  updateDueJobs_other: '{count} automations are due within 10 minutes:',
  updateNothingRunning: 'Nothing is running right now.',
  updateApply: 'Update and restart',
  updateFailed: 'Update failed: {message}',

  disconnect: 'Disconnect',
  disconnected: 'Disconnected. Use Reconnect to connect again.',
  settingsTokenOptional: 'Optional: this gateway does not require a token.',
  settingsUnreachable:
    'Could not reach {host}. The previous address is kept until a gateway answers.',
} as const)
