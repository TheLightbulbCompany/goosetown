import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// https://vitejs.dev/config/
export default defineConfig({
  base: '/ai-town',
  plugins: [react()],
  resolve: {
    alias: {
      'convex/react': path.resolve(__dirname, 'convex/isol8/react.ts'),
      'convex/values': path.resolve(__dirname, 'convex/isol8/values.ts'),
      'convex/server': path.resolve(__dirname, 'convex/isol8/server.ts'),
      'convex/react-clerk': path.resolve(__dirname, 'convex/isol8/react.ts'),
    },
  },
  server: {
    allowedHosts: ['ai-town-your-app-name.fly.dev', 'localhost', '127.0.0.1'],
    headers: {
      // Required for Godot HTML5 export with SharedArrayBuffer (if using threads).
      // Using --no-threads export for now, so these are optional but future-proof.
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
  },
});
