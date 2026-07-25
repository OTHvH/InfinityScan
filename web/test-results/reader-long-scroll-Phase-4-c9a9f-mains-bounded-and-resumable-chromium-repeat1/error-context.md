# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: reader-long-scroll.spec.ts >> Phase 4 long scroll remains bounded and resumable
- Location: e2e/reader-long-scroll.spec.ts:139:5

# Error details

```
Error: expect(received).toBeLessThanOrEqual(expected)

Expected: <= 1
Received:    2
```

# Page snapshot

```yaml
- generic [active] [ref=e1]:
  - alert [ref=e2]
  - generic [ref=e3]:
    - generic [ref=e6]:
      - button "Back" [ref=e7] [cursor=pointer]
      - generic [ref=e8]: Phase 4 Chapter 209.5
      - generic [ref=e9]: 10 / 10
      - group "Reading mode" [ref=e10]:
        - button "Continuous" [pressed] [ref=e11] [cursor=pointer]
        - button "Vertical chapter" [ref=e12] [cursor=pointer]
        - button "Horizontal" [ref=e13] [cursor=pointer]
      - generic "Page zoom" [ref=e14]:
        - button "Zoom out" [ref=e15] [cursor=pointer]: "-"
        - generic [ref=e16]: 100%
        - button "Zoom in" [ref=e17] [cursor=pointer]: +
        - button "Fit width" [pressed] [ref=e18] [cursor=pointer]
        - button "Reading direction" [ref=e19] [cursor=pointer]: LTR
      - generic "Chapter navigation" [ref=e20]:
        - button "Previous chapter" [ref=e21] [cursor=pointer]
        - button "Next chapter" [disabled] [ref=e22]
    - generic [ref=e23]:
      - generic [ref=e24]: Phase 4 Chapter 209.5
      - status [ref=e25]: saved
    - generic [ref=e26]: Left and right arrows change pages in paged modes. Space advances a page or spread. Left bracket opens the previous chapter and right bracket opens the next chapter. Shortcuts do not run while editing a form field.
    - img "Chapter 209.5, page 10" [ref=e33]
    - button "InfinityScan" [ref=e36] [cursor=pointer]
```

# Test source

```ts
  257 |   await expect(page.locator('.reader-page-frame[data-image-state="loaded"]').first()).toBeVisible();
  258 |   await page.waitForTimeout(100);
  259 |   const pageInput = page.getByRole("spinbutton", { name: "Current page" });
  260 |   const currentPageValue = Number(await pageInput.inputValue());
  261 |   failNextMedia = true;
  262 |   await pageInput.fill(currentPageValue >= PAGES_PER_CHAPTER ? "1" : String(currentPageValue + 1));
  263 |   await expect.poll(() => temporaryFailures).toBe(1);
  264 |   await expect.poll(() => mediaRetryRequests).toBeGreaterThanOrEqual(1);
  265 |   await expect(page.locator('.reader-page-frame[data-image-state="loaded"]').first()).toBeVisible();
  266 |   await page.getByRole("button", { name: "Continuous" }).click();
  267 |   await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  268 | 
  269 |   const furthestChapterIndex = await scrollToChapter(page, chapters, 105, "next", async () => {
  270 |     peakHeap = Math.max(peakHeap, await heapUsed(cdp));
  271 |   });
  272 |   await page.waitForTimeout(1800);
  273 |   await assertReaderGates(page);
  274 |   const atHundred = await diagnostics(page);
  275 |   const visibleChapterId = String(atHundred.visibleChapterId);
  276 |   const visiblePageNumber = Number(atHundred.visiblePageNumber);
  277 |   expect(furthestChapterIndex).toBeGreaterThanOrEqual(105);
  278 |   await expect(page).toHaveURL(new RegExp(`/reader/phase4-reader/${visibleChapterId}$`));
  279 |   const savedProgress = await page.evaluate(({ slug, chapterId }) => {
  280 |     const raw = localStorage.getItem(`infinityscan_progress_${slug}_${chapterId}`);
  281 |     return raw ? JSON.parse(raw) : null;
  282 |   }, { slug: "phase4-reader", chapterId: visibleChapterId });
  283 |   expect(savedProgress).toMatchObject({ chapterId: visibleChapterId, page: visiblePageNumber });
  284 | 
  285 |   await cdp.send("HeapProfiler.collectGarbage");
  286 |   const postEvictionHeap = await heapUsed(cdp);
  287 |   peakHeap = Math.max(peakHeap, postEvictionHeap);
  288 |   console.log(JSON.stringify({
  289 |     phase4MemoryBytes: { baseline: baselineHeap, peak: peakHeap, postEviction: postEvictionHeap },
  290 |     phase4MemoryMiB: {
  291 |       baseline: Number((baselineHeap / 1024 / 1024).toFixed(2)),
  292 |       peak: Number((peakHeap / 1024 / 1024).toFixed(2)),
  293 |       postEviction: Number((postEvictionHeap / 1024 / 1024).toFixed(2)),
  294 |     },
  295 |     heapGrowthLimitBytes: HEAP_GROWTH_LIMIT_BYTES,
  296 |   }));
  297 |   expect(postEvictionHeap).toBeLessThanOrEqual(baselineHeap + HEAP_GROWTH_LIMIT_BYTES);
  298 | 
  299 |   await page.goBack();
  300 |   await expect(page).toHaveURL(/\/login$/);
  301 |   await page.goForward();
  302 |   await expect(page.getByTestId("reader-diagnostics")).toBeAttached();
  303 |   await page.reload();
  304 |   await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  305 |   await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(visibleChapterId);
  306 |   const resumed = await diagnostics(page);
  307 |   expect(Math.abs(Number(resumed.visiblePageNumber) - visiblePageNumber)).toBeLessThanOrEqual(2);
  308 | 
  309 |   const previousBefore = previousChunkRequests;
  310 |   const currentIndex = chapterIndexById.get(visibleChapterId) ?? 105;
  311 |   await scrollToChapter(page, chapters, Math.max(0, currentIndex - 6), "previous");
  312 |   expect(previousChunkRequests).toBeGreaterThan(previousBefore);
  313 |   await page.waitForTimeout(300);
  314 |   const afterBackward = await diagnostics(page);
  315 |   const transitionChapterId = String(afterBackward.visibleChapterId);
  316 |   await page.getByRole("button", { name: "Horizontal" }).click();
  317 |   await expect(page.locator('.reader[data-reading-mode="horizontal"]')).toBeVisible();
  318 |   await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(transitionChapterId);
  319 |   await page.getByRole("button", { name: "Continuous" }).click();
  320 |   await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  321 |   await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(transitionChapterId);
  322 | 
  323 |   const exhaustedAt = await scrollToChapter(page, chapters, CHAPTER_COUNT - 1, "next");
  324 |   expect(exhaustedAt).toBe(CHAPTER_COUNT - 1);
  325 |   const finalChapterId = chapters.at(-1)?.id ?? "";
  326 |   await page.getByRole("button", { name: "Horizontal" }).click();
  327 |   await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(finalChapterId);
  328 |   await page.getByRole("button", { name: "Continuous" }).click();
  329 |   await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(finalChapterId);
  330 |   await expect(page).toHaveURL(new RegExp(`/reader/phase4-reader/${finalChapterId}$`));
  331 |   await expect.poll(async () => {
  332 |     const state = await diagnostics(page);
  333 |     return Number(state.activeNext) + Number(state.activePrevious);
  334 |   }).toBe(0);
  335 |   let stableRequestSamples = 0;
  336 |   let lastRequestCount = -1;
  337 |   for (let sample = 0; sample < 20 && stableRequestSamples < 3; sample += 1) {
  338 |     await page.waitForTimeout(250);
  339 |     const requestCount = [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0);
  340 |     stableRequestSamples = requestCount === lastRequestCount ? stableRequestSamples + 1 : 0;
  341 |     lastRequestCount = requestCount;
  342 |   }
  343 |   expect(stableRequestSamples).toBeGreaterThanOrEqual(3);
  344 |   const exhaustionCounts = new Map(chunkRequestCounts);
  345 |   const requestsAtExhaustion = [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0);
  346 |   const finalScroller = page.locator("[data-virtuoso-scroller]");
  347 |   for (let attempt = 0; attempt < 5; attempt += 1) {
  348 |     await finalScroller.evaluate((element) => element.scrollTo({ top: element.scrollHeight, behavior: "instant" }));
  349 |     await page.waitForTimeout(100);
  350 |   }
  351 |   const requestsAfterExhaustion = [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0);
  352 |   console.log(JSON.stringify({
  353 |     phase4ExhaustionRequests: [...chunkRequestCounts.entries()].filter(([key, count]) => count > (exhaustionCounts.get(key) ?? 0)),
  354 |   }));
  355 |   expect(requestsAfterExhaustion).toBe(requestsAtExhaustion);
  356 | 
> 357 |   expect(maxActiveNext).toBeLessThanOrEqual(1);
      |                         ^ Error: expect(received).toBeLessThanOrEqual(expected)
  358 |   expect(maxActivePrevious).toBeLessThanOrEqual(1);
  359 |   expect(temporaryFailures).toBe(1);
  360 |   expect(mediaRetryRequests).toBeGreaterThanOrEqual(1);
  361 |   expect(mediaRetryRequests).toBeLessThanOrEqual(3);
  362 |   expect(mediaRequests).toBeLessThan(CHAPTER_COUNT * PAGES_PER_CHAPTER);
  363 |   console.log(JSON.stringify({
  364 |     phase4Network: {
  365 |       chunkRequests: [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0),
  366 |       repeatedChunkRequests: [...chunkRequestCounts.entries()].filter(([, count]) => count > 1),
  367 |       mediaRequests,
  368 |       mediaRetryRequests,
  369 |       maxActiveNext,
  370 |       maxActivePrevious,
  371 |     },
  372 |   }));
  373 |   expect([...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0)).toBeLessThan(180);
  374 |   await assertReaderGates(page);
  375 | });
  376 | 
```