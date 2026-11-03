// https://docs.expo.dev/guides/using-eslint/
const { defineConfig } = require('eslint/config');
const expoConfig = require('eslint-config-expo/flat');

module.exports = defineConfig([
  expoConfig,
  {
    ignores: ['dist/*'],
    rules: {
      // This existing app uses manual animation, imperative audio queues, and ref-backed
      // event handlers. Keep compiler migration diagnostics visible without making that
      // separate migration a prerequisite for adopting lint. Hook order stays enforced.
      'react-hooks/refs': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/purity': 'warn',
      'react-hooks/static-components': 'warn',
      'react-hooks/preserve-manual-memoization': 'warn',
      // Apostrophes and quotes are ordinary copy in native Text nodes.
      'react/no-unescaped-entities': ['error', { forbid: ['>', '}'] }],
    },
  }
]);
