import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// svelte-check reads this to resolve preprocessing. Without it the diagnostics
// run without knowing about the `lang="ts"` blocks and report phantom errors.
export default {
  preprocess: vitePreprocess()
};
