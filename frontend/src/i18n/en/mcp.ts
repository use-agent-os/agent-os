import { defineNamespace } from '../registry'

export const mcp = defineNamespace('mcp', {
  // Validation (logic.ts).
  errorName: 'Enter a server name.',
  errorNameCharset: 'Use letters, numbers, dots, underscores, or hyphens.',
  errorNameTaken: 'This server name already exists.',
  errorCommand: 'Enter a command.',
  errorUrlScheme: 'Use an HTTP or HTTPS URL.',
  errorUrl: 'Enter a valid absolute URL.',
  errorHeadersObject: 'Headers must be a JSON object.',
  errorHeaderValues: 'Header values must be strings.',
  errorHeadersJson: 'Enter valid JSON.',
  errorTimeout: 'Choose a timeout from 1 to 600 seconds.',

  // Transport + detail labels (logic.ts).
  transportStreamableHttp: 'Streamable HTTP',
  transportSse: 'SSE',
  transportStdio: 'Local process',
  detailIncomplete: 'Configuration incomplete',

  // Per-server presentation (logic.ts).
  statePaused: 'Paused',
  statePausedDetail: 'Runtime disabled',
  stateUnavailable: 'Status unavailable',
  stateUnavailableDetail: 'Live gateway status is unavailable',
  stateConnected: 'Connected',
  stateConnectedDetail_one: '{count} registered tool',
  stateConnectedDetail_other: '{count} registered tools',
  stateAuthorization: 'Authorization required',
  stateAuthorizationDetail: 'Sign in before tools can load',
  stateOffline: 'Disconnected',
  stateOfflineDetail: 'Ready to connect',

  // Robinhood presentation (logic.ts).
  rhReady: 'Ready to connect',
  rhReadyDetail: 'Secure setup ready',
  rhReadyTools: 'Discovered on connect',
  rhReadyAction: 'Connect Robinhood',
  rhPaused: 'Runtime paused',
  rhPausedDetail: 'Configured and paused',
  rhPausedTools: 'Available when enabled',
  rhReviewAction: 'Review connection',
  rhUnavailable: 'Status unavailable',
  rhUnavailableDetail: 'Configuration is still available',
  rhUnavailableTools: 'Live discovery unavailable',
  rhConnected: 'Connected',
  rhConnectedDetail_one: '{count} live tool',
  rhConnectedDetail_other: '{count} live tools',
  rhConnectedTools: '{count} registered',
  rhManageAction: 'Manage connection',
  rhOauthRequired: 'OAuth required',
  rhOauthDetail: 'Ready for authorization',
  rhOauthTools: 'Loads after authorization',
  rhAuthorizeAction: 'Authorize Robinhood',
  rhSavedDetail: 'Configuration saved',
} as const)
