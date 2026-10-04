import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// The UI talks to one plugin's API. The port is that plugin's, and the proxy
// keeps the browser same-origin in development so CORS never masks an API bug.
export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5174,
    proxy: {
      '/api': {
        target: 'http://localhost:8004',
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/api/, '')
      }
    }
  },
  build: { outDir: 'dist', sourcemap: true }
});
