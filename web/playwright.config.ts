import { defineConfig, devices } from '@playwright/test'

const API_URL = process.env.API_URL ?? 'http://localhost:8000'
const WEB_URL = process.env.WEB_URL ?? 'http://localhost:3000'

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
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
  webServer: undefined, // User must start backend+frontend separately
})
