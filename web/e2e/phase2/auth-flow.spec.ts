import { test, expect, type Page } from '@playwright/test'

const API_URL = process.env.API_URL ?? 'http://localhost:8000'

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Register a user via the API + CSRF flow (bypasses UI for speed). */
async function registerUser(
  page: Page,
  username: string,
  password: string,
  email?: string,
): Promise<void> {
  // Fetch CSRF token
  const csrfResp = await page.request.get(`${API_URL}/auth/csrf`)
  const { csrf_token } = await csrfResp.json()

  // Register
  const body: Record<string, string> = { username, password }
  if (email) body.email = email

  const regResp = await page.request.post(`${API_URL}/auth/register`, {
    headers: { 'X-CSRF-Token': csrf_token },
    data: body,
  })
  expect(regResp.status()).toBe(201)
  const meResp = await page.request.get(`${API_URL}/auth/me`)
  expect(meResp.status()).toBe(200)
  expect((await meResp.json()).username).toBe(username)
}

/** Login via the UI. */
async function loginViaUI(
  page: Page,
  username: string,
  password: string,
): Promise<void> {
  await page.goto('/login')
  await page.fill('#login-identifier', username)
  await page.fill('#login-password', password)
  await page.click('button[type="submit"]')
  // Wait for navigation away from /login
  await page.waitForURL((url) => !url.pathname.includes('/login'), {
    timeout: 10_000,
  })
}

/** Logout via the API. */
async function logoutViaAPI(page: Page): Promise<void> {
  // Get CSRF token from cookie
  const csrf = await page.evaluate(() => {
    const match = document.cookie.match(/(^| )is_csrf=([^;]*)/)
    return match ? decodeURIComponent(match[2]) : ''
  })

  await page.request.post(`${API_URL}/auth/logout`, {
    headers: { 'X-CSRF-Token': csrf },
  })
}

// ── E2E Auth Flow ────────────────────────────────────────────────────────────

const USER_A = {
  username: `e2e_user_a_${Date.now()}`,
  password: 'StrongPass123!',
}

const USER_B = {
  username: `e2e_user_b_${Date.now()}`,
  password: 'StrongPass456!',
}

test.describe('Complete authentication and data isolation flow', () => {
  test('1-11: Register, bookmark, progress, logout, isolate, re-login, verify', async ({
    page,
  }) => {
    // ── Step 1: Register User A via API ───────────────────────────────────
    await registerUser(page, USER_A.username, USER_A.password)

    // ── Step 2: Use the authenticated registration session ────────────────
    await page.goto('/')
    await expect(page.locator('.brand-title')).toContainText('InfinityScan')

    // Add a bookmark via API (since we need a valid series)
    const csrfA = await page.evaluate(() => {
      const match = document.cookie.match(/(^| )is_csrf=([^;]*)/)
      return match ? decodeURIComponent(match[2]) : ''
    })

    const bookmarkResp = await page.request.post(`${API_URL}/bookmarks`, {
      headers: { 'X-CSRF-Token': csrfA },
      data: { series_path_word: 'test-series', series_name: 'Test Series' },
    })
    expect(bookmarkResp.status()).toBe(201)

    // Verify bookmark exists
    const listResp = await page.request.get(`${API_URL}/bookmarks`)
    expect(listResp.status()).toBe(200)
    const bookmarks = await listResp.json()
    expect(bookmarks.length).toBeGreaterThanOrEqual(1)
    expect(bookmarks[0].series_path_word).toBe('test-series')

    // ── Step 3: Save progress ────────────────────────────────────────────
    const chapterUuid = '00000000-0000-0000-0000-000000000001'
    const progressResp = await page.request.post(
      `${API_URL}/progress/test-series/${chapterUuid}`,
      {
        headers: { 'X-CSRF-Token': csrfA },
        data: { chapter_uuid: chapterUuid, last_page: 15, completed: false },
      },
    )
    expect(progressResp.status()).toBe(200)

    // Verify progress
    const getProgressResp = await page.request.get(
      `${API_URL}/progress/test-series/${chapterUuid}`,
    )
    expect(getProgressResp.status()).toBe(200)
    const progress = await getProgressResp.json()
    expect(progress.last_page).toBe(15)

    // ── Step 4: Log out ──────────────────────────────────────────────────
    await logoutViaAPI(page)

    // Verify /auth/me returns 401
    const meResp = await page.request.get(`${API_URL}/auth/me`)
    expect(meResp.status()).toBe(401)

    // ── Step 5: Register User B ──────────────────────────────────────────
    await registerUser(page, USER_B.username, USER_B.password)

    // ── Step 6: Confirm User A data is absent for User B ─────────────────

    // User B should see no bookmarks
    const bmResp = await page.request.get(`${API_URL}/bookmarks`)
    expect(bmResp.status()).toBe(200)
    const bmData = await bmResp.json()
    expect(bmData.length).toBe(0)

    // User B should see empty progress for the same chapter
    const progResp = await page.request.get(
      `${API_URL}/progress/test-series/${chapterUuid}`,
    )
    expect(progResp.status()).toBe(200)
    const progData = await progResp.json()
    expect(progData.last_page).toBeNull()

    // ── Step 7: Log out User B ───────────────────────────────────────────
    await logoutViaAPI(page)

    // ── Step 8: Log in as User A ─────────────────────────────────────────
    await loginViaUI(page, USER_A.username, USER_A.password)

    // ── Step 9: Confirm bookmark and progress remain ──────────────────────
    const bmResp2 = await page.request.get(`${API_URL}/bookmarks`)
    expect(bmResp2.status()).toBe(200)
    const bmData2 = await bmResp2.json()
    expect(bmData2.length).toBeGreaterThanOrEqual(1)
    expect(bmData2[0].series_path_word).toBe('test-series')

    const progResp2 = await page.request.get(
      `${API_URL}/progress/test-series/${chapterUuid}`,
    )
    expect(progResp2.status()).toBe(200)
    const progData2 = await progResp2.json()
    expect(progData2.last_page).toBe(15)

    // ── Step 10: Confirm logout removes access ────────────────────────────
    await logoutViaAPI(page)

    const meResp2 = await page.request.get(`${API_URL}/auth/me`)
    expect(meResp2.status()).toBe(401)

    // ── Step 11: Confirm unauthenticated mutation fails ────────────────────
    const unauthBmResp = await page.request.post(`${API_URL}/bookmarks`, {
      data: { series_path_word: 'evil', series_name: 'Evil' },
    })
    expect(unauthBmResp.status()).toBe(401)

    const unauthProgResp = await page.request.post(
      `${API_URL}/progress/evil/${chapterUuid}`,
      {
        data: { chapter_uuid: chapterUuid, last_page: 999 },
      },
    )
    expect(unauthProgResp.status()).toBe(401)
  })
})
