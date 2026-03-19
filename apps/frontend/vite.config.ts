import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig({
  base: '/',
  plugins: [react()],
  server: {
    allowedHosts: ['localhost', '127.0.0.1'],
    headers: {
      // Required for Godot HTML5 export with SharedArrayBuffer (if using threads).
      // Using --no-threads export for now, so these are optional but future-proof.
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
  },
});
