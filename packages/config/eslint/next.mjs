// Next.js App Router layer.
//
// `eslint-config-next` is intentionally not pulled in: it is still an eslintrc
// bundle behind a FlatCompat shim, drags in a second copy of the TS parser, and
// most of what it adds duplicates checks the Next compiler already performs.
// What is genuinely valuable — stopping a server secret from reaching the client
// bundle — is expressed directly below.

import { reactConfig } from './react.mjs';

export const nextConfig = [
  ...reactConfig,
  {
    files: ['**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [
            {
              name: 'next/router',
              message: 'App Router: use next/navigation.',
            },
          ],
          patterns: [
            {
              group: ['../../../../*'],
              message: 'Four levels up means the module belongs somewhere else. Use the @/ alias.',
            },
          ],
        },
      ],
    },
  },
  {
    // Client Components: a `process.env.X` read here is inlined at build time and
    // shipped to every visitor, so only NEXT_PUBLIC_* may appear. Anything else
    // is a secret leak waiting to happen and must move to a Server Component,
    // a Route Handler or a Server Action.
    files: ['**/*.tsx'],
    rules: {
      'no-restricted-syntax': [
        'error',
        {
          selector:
            "MemberExpression[object.object.name='process'][object.property.name='env'][property.name!=/^NEXT_PUBLIC_/][property.name!='NODE_ENV']",
          message:
            'Only NEXT_PUBLIC_* env vars may be referenced from a component file — everything else is inlined into the browser bundle. Read it in a Server Component, Route Handler or Server Action.',
        },
      ],
    },
  },
  {
    // The generated service worker and PWA glue run outside the bundler.
    files: ['**/public/**/*.js', '**/*.worker.{ts,js}'],
    languageOptions: { sourceType: 'script' },
    rules: { 'no-console': 'off', '@typescript-eslint/no-unused-vars': 'off' },
  },
];

export default nextConfig;
