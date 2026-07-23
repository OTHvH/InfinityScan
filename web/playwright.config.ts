import { defineConfig, devices } from '@playwright/test'

const WEB_URL = process.env.WEB_URL ?? 'http://localhost:3000'

export default defineConfig({
  testDir: './e2e',
  timeout: 180_000,
  retries: 0,
  use: {
    baseURL: WEB_URL,
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: process.env.PLAYWRIGHT_EXTERNAL_SERVER === '1' ? undefined : [
    {
      command: 'PYTHONPATH=../api DATABASE_URL=sqlite:////tmp/opencode/infinityscan-playwright.db APP_ENV=development OBJECT_STORAGE_ENABLED=false JWT_SECRET_KEY=phase4-reader-test-secret-012345678901234567890123456789 CSRF_SECRET_KEY=phase4-csrf-test-secret-012345678901234567890123456789 ../.venv/bin/python -c "from database import drop_all_tables, create_all_tables; drop_all_tables(); create_all_tables()" && PYTHONPATH=../api DATABASE_URL=sqlite:////tmp/opencode/infinityscan-playwright.db APP_ENV=development OBJECT_STORAGE_ENABLED=false JWT_SECRET_KEY=phase4-reader-test-secret-012345678901234567890123456789 CSRF_SECRET_KEY=phase4-csrf-test-secret-012345678901234567890123456789 ../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000',
      url: 'http://localhost:8000/health',
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: 'npm run start',
      url: WEB_URL,
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
})
