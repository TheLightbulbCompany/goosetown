/// <reference types="vitest" />
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  // Cast avoids a spurious type clash: vitest 1.x resolves vite 5.x types
  // while the app's own vite is 4.x, so the Plugin<any> identity differs.
  // Runtime behaviour is identical.
  plugins: [react() as any],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
});
