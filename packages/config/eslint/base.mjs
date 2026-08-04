// Shared ESLint flat config — TypeScript baseline.
//
// Deliberately NOT type-aware (`projectService`): type-aware linting needs a
// resolved program per file, which triples lint time on a monorepo this size and
// duplicates work `tsc --noEmit` already does in `pnpm typecheck`. The rules kept
// here are the ones a type checker cannot express.

import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';

/** Files nothing should ever lint. */
export const ignores = [
  '**/node_modules/**',
  '**/dist/**',
  '**/.next/**',
  '**/.turbo/**',
  '**/coverage/**',
  '**/playwright-report/**',
  '**/test-results/**',
  '**/*.d.ts',
  '**/next-env.d.ts',
];

export const baseConfig = tseslint.config(
  { ignores },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node, ...globals.es2022 },
    },
    rules: {
      // `_foo` is the agreed "deliberately unused" marker; it matches the
      // tsconfig's noUnusedParameters escape hatch so the two agree.
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          destructuredArrayIgnorePattern: '^_',
        },
      ],
      '@typescript-eslint/consistent-type-imports': [
        'error',
        { prefer: 'type-imports', fixStyle: 'inline-type-imports' },
      ],
      '@typescript-eslint/no-explicit-any': 'warn',
      // `console.log` in shipped code is a leak risk (tokens, PII) and noise;
      // warn/error are legitimate.
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      eqeqeq: ['error', 'smart'],
      'no-implicit-coercion': 'error',
      'prefer-const': ['error', { destructuring: 'all' }],
      'object-shorthand': 'error',
      'no-restricted-globals': [
        'error',
        { name: 'event', message: 'Take the event as a parameter instead.' },
      ],
    },
  },
  {
    files: ['**/*.test.ts', '**/*.test.tsx', '**/*.spec.ts', '**/*.spec.tsx', '**/mocks/**'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      'no-console': 'off',
    },
  },
);

export default baseConfig;
