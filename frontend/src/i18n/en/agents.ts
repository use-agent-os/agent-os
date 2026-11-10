import { defineNamespace } from '../registry'

export const agents = defineNamespace('agents', {
  // Create / edit dialog.
  fieldIdRequired: 'Agent ID is required.',
} as const)
