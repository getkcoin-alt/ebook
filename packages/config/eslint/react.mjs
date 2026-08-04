// React layer on top of the base config.
//
// The react-hooks plugin is wired by hand rather than through its exported
// preset: the preset's key has been renamed twice across major versions
// (`recommended` -> `flat.recommended` -> `recommended-latest`), and pinning to
// rule names instead of a preset name makes the config survive that churn.

import reactPlugin from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';

import { baseConfig } from './base.mjs';

export const reactConfig = [
  ...baseConfig,
  {
    files: ['**/*.{ts,tsx,js,jsx,mjs}'],
    plugins: { react: reactPlugin, 'react-hooks': reactHooks },
    languageOptions: {
      globals: { ...globals.browser, ...globals.serviceworker },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: 'detect' } },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react/jsx-key': ['error', { checkFragmentShorthand: true }],
      'react/jsx-no-target-blank': ['error', { allowReferrer: false }],
      'react/no-danger-with-children': 'error',
      'react/self-closing-comp': 'error',
      // React 17+ automatic runtime: neither is needed.
      'react/react-in-jsx-scope': 'off',
      'react/prop-types': 'off',
    },
  },
];

export default reactConfig;
