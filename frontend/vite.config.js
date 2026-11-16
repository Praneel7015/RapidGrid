import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// Preserve the browser's Host through the development proxy so same-origin
// session/mutation checks see the real frontend origin. The override is only
// read by the development server (it is never bundled into browser code).
const apiTarget = process.env.RAPIDGRID_DEV_API_URL || 'http://localhost:8000'

// Server routes the service worker must never answer from a cache or with the
// app shell: live incidents, chat, sessions, health and API docs.
const SERVER_ONLY = [/^\/api(\/|$)/, /^\/ws\//, /^\/health(\/|$)/, /^\/docs/, /^\/redoc/, /^\/openapi\.json/]

const pwa = VitePWA({
  // A new version waits until the user chooses to reload, so an open call is
  // never interrupted by an automatic refresh.
  registerType: 'prompt',
  injectRegister: false,
  includeAssets: ['favicon.svg', 'apple-touch-icon.png'],
  manifest: {
    id: '/',
    name: 'RapidGrid',
    short_name: 'RapidGrid',
    description: 'Bengaluru emergency dispatch simulator: report, dispatch, drive and receive.',
    start_url: '/',
    scope: '/',
    display: 'standalone',
    orientation: 'any',
    theme_color: '#e8a020',
    background_color: '#19181c',
    icons: [
      { src: '/pwa-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/pwa-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/pwa-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
    shortcuts: [
      { name: 'Report an emergency', short_name: 'Report', url: '/citizen' },
      { name: 'City dispatch', short_name: 'Dispatch', url: '/dispatcher' },
      { name: 'Field unit', short_name: 'Driver', url: '/driver' },
      { name: 'Emergency department', short_name: 'Hospital', url: '/hospital' },
    ],
  },
  workbox: {
    // The app shell only. The in-browser speech model (transformers chunk and
    // a ~21 MB wasm file) loads on demand, never at install.
    globPatterns: ['**/*.{js,css,html,svg,png,ico}'],
    globIgnores: ['**/transformers*.js', '**/*.wasm'],
    navigateFallback: '/index.html',
    navigateFallbackDenylist: SERVER_ONLY,
    cleanupOutdatedCaches: true,
    runtimeCaching: [
      {
        // Live data is never cached: if the network is down the request fails
        // and the page says so, instead of showing old incidents.
        // Inlined: Workbox serialises this function into sw.js, so it cannot
        // refer to SERVER_ONLY above.
        urlPattern: ({ url, sameOrigin }) =>
          sameOrigin && /^\/(api(\/|$)|ws\/|health(\/|$)|docs|redoc|openapi\.json)/.test(url.pathname),
        handler: 'NetworkOnly',
      },
      {
        urlPattern: ({ url }) => url.origin === 'https://fonts.googleapis.com',
        handler: 'StaleWhileRevalidate',
        options: { cacheName: 'google-fonts-css' },
      },
      {
        urlPattern: ({ url }) => url.origin === 'https://fonts.gstatic.com',
        handler: 'CacheFirst',
        options: {
          cacheName: 'google-fonts',
          expiration: { maxEntries: 20, maxAgeSeconds: 60 * 60 * 24 * 365 },
          cacheableResponse: { statuses: [0, 200] },
        },
      },
      {
        // Map tiles the user has already viewed, kept for a week.
        // Exact host: the map loads tiles only from tile.openstreetmap.org.
        urlPattern: ({ url }) => url.origin === 'https://tile.openstreetmap.org',
        handler: 'CacheFirst',
        options: {
          cacheName: 'map-tiles',
          expiration: { maxEntries: 400, maxAgeSeconds: 60 * 60 * 24 * 7 },
          cacheableResponse: { statuses: [0, 200] },
        },
      },
    ],
  },
})

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), pwa],
  optimizeDeps: {
    exclude: ['@huggingface/transformers'],
  },
  server: {
    proxy: {
      '/api': { target: apiTarget, changeOrigin: false },
      '/health': { target: apiTarget, changeOrigin: false },
      '/ws': { target: apiTarget.replace(/^http/, 'ws'), changeOrigin: false, ws: true },
    }
  }
})
