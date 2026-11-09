import { defineNamespace } from '../registry'

/** Copy of the rebuilt web console's Settings hub. */
export const webSettings = defineNamespace('webSettings', {
  mcpOAuthWorking: 'Finishing MCP authorization…',
  mcpOAuthFailed: 'MCP authorization failed',
  mcpOAuthMissing: 'The callback is missing its authorization code or state.',
  mcpOAuthBack: 'Back to MCP settings',
  mcpOAuthSucceeded: 'MCP server authorized',
} as const)
