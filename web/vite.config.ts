import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 本地开发时把 /api 代理到 FastAPI；生产用 nginx 反代
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
