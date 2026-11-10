import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

// Files whose user-facing copy has been moved into src/i18n (issue #138).
// This list IS the migration ledger: a view joins it in the same change that
// extracts its strings, and can never silently regress afterwards. Views that
// have not been migrated yet stay out, so the rule below is never noise.
const I18N_MIGRATED = [
  'src/app/**/*.tsx',
  'src/components/**/*.tsx',
  'src/views/chat/**/*.tsx',
]

// #258: t() reads the locale at call time, so calling it at module scope
// resolves the string once — at module evaluation — and freezes it for the life
// of the page. Every user-facing string must be produced inside a function (a
// component, a helper, a getter) that re-runs after a locale change. The :not()
// clauses spell out "this call has no function ancestor".
const NO_MODULE_SCOPE_T = {
  selector: [
    'CallExpression[callee.name=/^t(Plural)?$/]',
    ':not(FunctionDeclaration CallExpression)',
    ':not(FunctionExpression CallExpression)',
    ':not(ArrowFunctionExpression CallExpression)',
    ':not(MethodDefinition CallExpression)',
    ':not(PropertyDefinition CallExpression)',
  ].join(''),
  message:
    'Module-scope t() freezes the string at module-evaluation time (#258). Move it inside a function so it re-resolves after a locale change.',
}

// Built-in no-restricted-syntax rather than react/jsx-no-literals: this repo
// has no eslint-plugin-react, and the esquery selectors below enforce the same
// thing without adding a dependency. The {3,} letter threshold keeps
// punctuation, separators, and units out of the catalog.
const NO_HARDCODED_COPY = [
  {
    selector: 'JSXText[value=/[A-Za-z]{3,}/]',
    message: 'User-facing text must come from t() — add it to src/i18n/en/<view>.ts.',
  },
  {
    selector:
      'JSXAttribute[name.name=/^(aria-label|aria-description|placeholder|title|alt)$/] > Literal[value=/[A-Za-z]{3,}/]',
    message: 'Accessible and attribute copy must come from t().',
  },
  {
    // JSXText only covers bare text nodes. Copy also reaches the DOM through
    // expression containers — `{busy ? 'Saving…' : 'Save'}`, `{x || 'None'}` —
    // which the rule above cannot see, and which is exactly how the skills
    // dialog's Update/Remove buttons survived the first migration pass.
    // The :not() excludes attribute values, so the `className={x ? 'a' : 'b'}`
    // ternaries all over this codebase stay legal. Only direct ternary branches
    // and `||` fallbacks, so a `{'main'}` identifier escape hatch still passes.
    selector: [
      'JSXExpressionContainer:not(JSXAttribute JSXExpressionContainer)',
      '> :matches(ConditionalExpression, LogicalExpression)',
      '> Literal[value=/[A-Za-z]{3,}/]',
    ].join(' '),
    message:
      'Copy inside a JSX expression must come from t() too — a ternary or || fallback still renders to the user.',
  },
]

// The rebuilt web console (openspec change overhaul-control-ui, D2) may lean on
// the kept transport and logic layers only — never on the legacy UI it replaces.
// Inside web/**, relative imports stay within the web tree (at most one `../`,
// which from any web/<area>/ file still lands inside web/); everything else goes
// through the `@/` alias, which is what the patterns below inspect.
const LEGACY = '^(@/|(\\.\\./){2,})'
const WEB_BOUNDARY = {
  paths: [
    {
      name: '@/views/chat/Attachments',
      allowImportNames: ['useAttachments'],
      message: 'Only the useAttachments hook is kept; build the tray in web/.',
    },
  ],
  patterns: [
    {
      regex: '^(\\.\\./){2,}',
      message: 'Leave the web tree through @/ (e.g. @/lib/…, @/web/…), not ../../.',
    },
    {
      regex: `${LEGACY}views/chat/useTranscript$`,
      message: 'The legacy DOM transcript engine is desktop-only. Use web/features/chat/model.',
    },
    {
      regex: `${LEGACY}views/chat/transcript/`,
      importNamePattern: '^create\\w*Renderer$',
      message: 'create*Renderer builds legacy DOM. Import only the pure helpers.',
    },
    {
      regex: `${LEGACY}views/.*Page$`,
      message: 'Legacy page components are being replaced, not reused.',
    },
    {
      regex: `${LEGACY}views/chat/(Composer|Toolbar|SlashMenu|RoutePicker|PendingQueue|ElevatedPill|SessionChip|ChatPage)$`,
      message: 'Legacy chat components are desktop-only. Use the kept hooks instead.',
    },
    {
      regex: `${LEGACY}app/(?!providers$)`,
      message: 'The legacy app shell is replaced by web/app and web/shell.',
    },
    {
      regex: `${LEGACY}components/(?!(KeyboardShortcuts|overlay-layer|shortcut-combo)$)`,
      message: 'Legacy components are replaced by web/components.',
    },
    {
      regex: '^(?!@/web/|\\.{1,2}/).*\\.css$',
      message: 'web/** may import only its own stylesheets (web/styles).',
    },
  ],
}

export default tseslint.config(
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks, 'react-refresh': reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['**/*.test.{ts,tsx}'],
    // no-restricted-syntax is replaced, not merged, by a later config that also
    // sets it — so the migrated-view block below repeats this entry.
    rules: { 'no-restricted-syntax': ['error', NO_MODULE_SCOPE_T] },
  },
  {
    files: I18N_MIGRATED,
    ignores: ['**/*.test.{ts,tsx}'],
    rules: { 'no-restricted-syntax': ['error', NO_MODULE_SCOPE_T, ...NO_HARDCODED_COPY] },
  },
  {
    files: ['src/web/**/*.{ts,tsx}'],
    ignores: ['**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', NO_MODULE_SCOPE_T, ...NO_HARDCODED_COPY],
      'no-restricted-imports': ['error', WEB_BOUNDARY],
    },
  },
  {
    // Tests may reach legacy fixtures and the legacy sanitizer (parity tests),
    // but never the legacy UI components themselves.
    files: ['src/web/**/*.test.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: WEB_BOUNDARY.paths,
          patterns: WEB_BOUNDARY.patterns.filter((p) => !p.regex.startsWith('^(\\.\\./)')),
        },
      ],
    },
  },
)
