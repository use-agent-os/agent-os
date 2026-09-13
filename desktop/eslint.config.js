import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

export default tseslint.config(
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
  {
    files: ['src/renderer/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks, 'react-refresh': reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    // The renderer must never import Node/Electron APIs directly: everything
    // crosses the bridge through the typed `window.agentos` preload surface.
    files: ['src/renderer/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [
            { name: 'electron', message: 'Use window.agentos (preload bridge) instead.' },
            { name: 'node:fs', message: 'Renderer has no filesystem access.' },
            { name: 'node:child_process', message: 'Renderer cannot spawn processes.' },
          ],
          patterns: ['../main/*', '@/../main/*'],
        },
      ],
    },
  },
)
