import { defineNamespace } from '../registry'

/**
 * The app shell: navigation, connection footer, route errors, the global
 * approval prompt, and the shared chrome components. View bodies live in their
 * own namespaces.
 */
export const shell = defineNamespace('shell', {
  // Brand. Not translated in practice, but routed through the catalog so the
  // shell has no literal copy left for a translator to miss.
  brandName: 'AgentOS',
  brandSuffix: 'Control',

  // View titles. One definition, read by both the router and the sidebar.
  viewAgents: 'Agents',
  viewApprovals: 'Approvals',
  viewChannels: 'Channels',
  viewChat: 'Chat',
  viewConfig: 'Config',
  viewCron: 'Cron',
  viewEnv: 'Environment',
  viewHealth: 'Health',
  viewLogs: 'Logs',
  viewMcp: 'MCP Servers',
  viewMemory: 'Memory',
  viewOverview: 'Overview',
  viewProjects: 'Projects',
  viewSessions: 'Sessions',
  viewSettings: 'Agent Setup',
  viewSetup: 'Setup',
  viewSkills: 'Skills',
  viewUsage: 'Usage',

  // Sidebar groups.
  navGroupChat: 'Chat',
  navGroupControl: 'Control',
  navGroupSettings: 'Settings',
  navLandmark: 'Main',
  navDrawerLandmark: 'Workspace navigation',
  navCollapse: 'Collapse navigation',
  navExpand: 'Expand navigation',
  navClose: 'Close navigation',
  navToggleMenu: 'Toggle menu',
  navSkipToContent: 'Skip to main content',
  navApprovalBadge_one: '{count} pending approval',
  navApprovalBadge_other: '{count} pending approvals',

  // Connection footer.
  connConnected: 'Connected',
  connConnecting: 'Connecting',
  connDisconnected: 'Disconnected',
  connWithVersion: '{state}, version {version}',
  connecting: 'Connecting…',

  // Sidebar utilities.
  shortcutsTitle: 'Keyboard shortcuts ({combo})',
  shortcutsLabel: 'Keyboard shortcuts',
  shortcutClose: 'Close dialog',
  shortcutDrawerEscape: 'Close the navigation drawer (mobile)',
  shortcutCategoryGlobal: 'Global',
  shortcutSeparator: 'then',
  shortcutCategoryNavigation: 'Navigation',
  shortcutCategoryChat: 'Chat',
  shortcutNewChat: 'Start a new chat',
  shortcutAbortTurn: 'Abort the streaming turn, else recover the queue into the composer',
  shortcutNavChat: 'Go to Chat',
  shortcutNavOverview: 'Go to Overview',
  shortcutNavHealth: 'Go to Health',
  shortcutNavChannels: 'Go to Channels',
  shortcutNavMcp: 'Go to MCP Servers',
  shortcutNavSkills: 'Go to Skills',
  shortcutNavProjects: 'Go to Projects',
  shortcutNavSessions: 'Go to Sessions',
  shortcutNavAgents: 'Go to Agents',
  shortcutNavUsage: 'Go to Usage',
  shortcutNavCron: 'Go to Cron',
  shortcutNavSettings: 'Go to Agent Setup',
  shortcutNavEnv: 'Go to Environment',
  shortcutNavLogs: 'Go to Logs',
  shortcutNavMemory: 'Go to Memory',
  shortcutNavApprovals: 'Go to Approvals',
  themeDark: 'dark',
  themeLight: 'light',
  themeTitle: 'Theme: {mode}',
  themeToggleLabel: 'Theme: {mode}. Toggle theme',

  // Chat surface header.
  chatToolbar: 'Chat toolbar',

  // Routing.
  routeNotFoundTitle: 'Not Found - AgentOS Control',
  routeNotFoundBody: 'Page not found: {path}',
  routePending: 'Opening view…',

  // Route error boundary.
  errorDocumentTitle: 'Recovery - AgentOS Control',
  errorHttpCode: 'HTTP {status}',
  errorViewCode: 'VIEW ERROR',
  errorUnavailableTitle: 'This view is unavailable',
  errorSnagTitle: 'This view hit a snag',
  errorMessage:
    'AgentOS kept the rest of your workspace intact. Reload this view or return to Overview.',
  errorEyebrow: 'Workspace recovery',
  errorStatus: 'View paused safely',
  errorDeveloperDetail: 'Developer detail',
  errorActions: 'Recovery actions',
  errorReload: 'Reload view',
  errorGoOverview: 'Go to Overview',

  // Global approval prompt.
  approvalTitle: 'Approval Required',
  approvalFallbackTool: 'Tool execution',
  approvalOnceTitle: 'Approve only this pending tool call',
  approvalOnce: 'Approve This Time',
  approvalAlwaysTitle: 'Remember this operation type for future matching intents',
  approvalAlways: 'Always Allow This Type',
  approvalBypassTitle:
    'Enable approval bypass in this browser session and approve this pending tool call',
  approvalBypass: 'Bypass Approvals',
  approvalDeny: 'Deny',

  // Command line with copy action.
  commandCopyLabel: 'Copy command',
  commandCopied: 'Copied command',
  commandCopyFailed: 'Copy failed: {message}',
  commandCopyError: 'Copy command failed',

  // Update banner.
  updateAvailable:
    'A new version of use-agent-os is available: {current} → {latest}. Run agentos upgrade.',
  updateDismiss: 'Dismiss',
  updateLabel: 'Update',
  updateNow: 'Update now',
  updateRetry: 'Retry',
  updateRunning: 'Updating AgentOS… the gateway restarts when the install finishes.',
  updateRunningShort: 'Updating…',
  updateDone: 'Updated to {latest}. Reload to load the new console.',
  updateFailed: 'Update failed: {detail}',
  updateReload: 'Reload',
} as const)
