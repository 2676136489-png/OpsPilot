import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Production serves the SPA from nginx, which proxies /api/ to the backend
    // (see Dockerfile). Dev needs the same wiring, otherwise the frontend's
    // relative /api/v1 requests 404 against the Vite dev server.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        configure: (proxy) => {
          // SSE frames must not be buffered or length-frozen by the proxy,
          // otherwise the agent timeline appears to stall.
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache'
              delete proxyRes.headers['content-length']
            }
          })
        },
      },
    },
  },
})
