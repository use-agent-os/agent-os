import { defineNamespace } from '../registry'

export const env = defineNamespace('env', {
  title: 'Environment',

  // Category labels.
  categoryProvider: 'LLM providers',
  categorySearch: 'Search',
  categoryImage: 'Image generation',
  categoryAudio: 'Audio',
  categoryMemory: 'Memory embedding',
  categorySkill: 'Skills',
  categoryCustom: 'Your own variables',

  // Where a value is coming from.
  sourceProcess: 'process env',
  sourceCwdFile: 'project .env',
  sourceHomeFile: 'AgentOS .env',

  // Summary strip.
  statSetTotal: '/{total}',
  shortPath: '…/{tail}',

  // Toolbar.
  search: 'Search variables',

  // List.
  groupCount: '{set}/{total} set',

  // Name validation.
  validateEmpty: 'Enter a variable name.',
  validateCharset: 'Use letters, digits, and underscores, starting with a letter or underscore.',
  validateReadOnly: 'This name cannot be written through AgentOS.',
} as const)
