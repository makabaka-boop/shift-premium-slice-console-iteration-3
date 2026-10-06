import { defineConfig } from '@playwright/test'

// 启动前自行跑：(cd backend && uvicorn app.main:app --port 8000) & (cd web && npm run dev)
// 或直接用 docker compose up 后把 baseURL 指向 http://localhost:8080
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8080',
  },
  retries: 0,
})
