import { expect, test, type Response } from "@playwright/test";

const API_URL = process.env.API_URL ?? "http://localhost:8000";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the mandatory Phase 3 browser test`);
  return value;
}

test("Phase 3 imported content is visible and private media resolves", async ({ page, request }) => {
  const slug = required("PHASE3_SERIES_SLUG");
  const readyChapterId = required("PHASE3_READY_CHAPTER_ID");
  const importingChapterId = required("PHASE3_IMPORTING_CHAPTER_ID");
  const failedChapterId = required("PHASE3_FAILED_CHAPTER_ID");
  const pendingPageId = required("PHASE3_PENDING_PAGE_ID");
  const quarantinedPageId = required("PHASE3_QUARANTINED_PAGE_ID");
  const mediaRequests: string[] = [];
  const responses: Response[] = [];

  await page.context().clearCookies();
  await page.addInitScript(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  page.on("request", (entry) => {
    if (new URL(entry.url()).pathname.startsWith("/media/pages/")) mediaRequests.push(entry.url());
  });
  page.on("response", (response) => responses.push(response));

  await page.goto("/");
  await expect(page.locator(`a[href="/series/${slug}"]`).first()).toBeVisible();
  await page.locator(`a[href="/series/${slug}"]`).first().click();

  await expect(page).toHaveURL(new RegExp(`/series/${slug}$`));
  await expect(page.getByRole("heading", { name: /^Chapters \([1-9]\d*\)$/ })).toBeVisible();
  const readyLink = page.locator(`a[href="/reader/${slug}/${readyChapterId}"]`);
  await expect(readyLink).toBeVisible();
  await expect(page.locator(`a[href="/reader/${slug}/${importingChapterId}"]`)).toHaveCount(0);
  await expect(page.locator(`a[href="/reader/${slug}/${failedChapterId}"]`)).toHaveCount(0);

  const chunkResponse = await request.get(
    `${API_URL}/reader/${slug}/chunks?direction=next&limit=2&start_chapter_id=${readyChapterId}`,
  );
  expect(chunkResponse.status()).toBe(200);
  const chunkText = await chunkResponse.text();
  const chunk = JSON.parse(chunkText) as { chapters: Array<{ pages: unknown[] }> };
  expect(chunk.chapters.length).toBeGreaterThan(0);
  expect(chunk.chapters[0].pages.length).toBeGreaterThan(0);
  expect(chunkText).not.toContain(importingChapterId);
  expect(chunkText).not.toContain(failedChapterId);
  expect(chunkText).not.toContain(pendingPageId);
  expect(chunkText).not.toContain(quarantinedPageId);
  expect(chunkText).not.toContain("object_key");
  expect(chunkText).not.toContain("X-Amz-");
  expect((await request.get(`${API_URL}/media/pages/${pendingPageId}`, { maxRedirects: 0 })).status()).toBe(404);
  expect((await request.get(`${API_URL}/media/pages/${quarantinedPageId}`, { maxRedirects: 0 })).status()).toBe(404);

  await readyLink.click();
  await expect(page.locator(".reader")).toBeVisible();
  await expect(page.locator('.reader-page-frame[data-image-state="loaded"]').first()).toBeVisible();

  expect(mediaRequests.some((url) => new URL(url).pathname.startsWith("/media/pages/"))).toBe(true);
  const redirect = responses.find((response) =>
    response.status() === 307 && new URL(response.url()).pathname.startsWith("/media/pages/"),
  );
  expect(redirect, "InfinityScan media endpoint must return a redirect").toBeDefined();
  const finalImage = responses.find((response) => {
    const redirectedFrom = response.request().redirectedFrom();
    return response.status() === 200
      && response.headers()["content-type"]?.startsWith("image/")
      && redirectedFrom?.url() === redirect?.url();
  });
  expect(finalImage, "presigned media redirect must finish with an image HTTP 200").toBeDefined();

  const html = await page.content();
  expect(html).not.toContain("X-Amz-Credential");
  expect(html).not.toContain("X-Amz-Signature");
  expect(html).not.toContain("minioadmin");
  expect(html).not.toContain("phase3/");
  expect(mediaRequests.some((url) => url.includes(pendingPageId) || url.includes(quarantinedPageId))).toBe(false);
});
