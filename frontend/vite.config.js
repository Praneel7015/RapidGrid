import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Preserve the browser's Host through the development proxy so same-origin
// session/mutation checks see the real frontend origin. The override is only
// read by the development server (it is never bundled into browser code).
const apiTarget = process.env.RAPIDGRID_DEV_API_URL || 'http://localhost:8000'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
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
