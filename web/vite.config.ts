import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    // 本地服务（python -m server.main，默认 8010）代理过去，避免 CORS
    proxy: { '/api': 'http://127.0.0.1:8010' },
  },
  preview: { port: 4173 },
})
