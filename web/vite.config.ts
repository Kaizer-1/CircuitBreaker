/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the page is served by Vite and /v1 is proxied to the gateway that `make up` starts on :8080.
// In Docker Compose, nginx (web/nginx.conf) does the same proxying.
// `npm run dev:mock` (with `make mock` running) proxies to the mock instead: Prism on :4010 for HTTP, generated from
// the specs, and web/mock/ws-mock.mjs on :4011 for the WebSocket channels.
export default defineConfig(({ mode }) => {
  const mock = mode === 'mock'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/v1/ws': { target: mock ? 'ws://localhost:4011' : 'ws://localhost:8080', ws: true },
        '/v1': mock ? 'http://localhost:4010' : 'http://localhost:8080',
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./test/setup.ts'],
    },
  }
})
