# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: reader-long-scroll.spec.ts >> Phase 4 long scroll remains bounded and resumable
- Location: e2e/reader-long-scroll.spec.ts:139:5

# Error details

```
Error: expect(received).toBeGreaterThanOrEqual(expected)

Expected: >= 1
Received:    0

Call Log:
- Timeout 5000ms exceeded while waiting on the predicate
```

# Page snapshot

```yaml
- generic [ref=e1]:
  - alert [ref=e2]
  - generic [ref=e3]:
    - generic [ref=e6]:
      - button "Back" [ref=e7] [cursor=pointer]
      - generic [ref=e8]: Phase 4 Chapter 34
      - generic [ref=e9]: 2 / 10
      - group "Reading mode" [ref=e10]:
        - button "Continuous" [ref=e11] [cursor=pointer]
        - button "Vertical chapter" [ref=e12] [cursor=pointer]
        - button "Horizontal" [pressed] [ref=e13] [cursor=pointer]
        - group "Horizontal page layout" [ref=e14]:
          - button "Single" [pressed] [ref=e15] [cursor=pointer]
          - button "Spread" [ref=e16] [cursor=pointer]
      - generic "Page zoom" [ref=e17]:
        - button "Zoom out" [ref=e18] [cursor=pointer]: "-"
        - generic [ref=e19]: 100%
        - button "Zoom in" [ref=e20] [cursor=pointer]: +
        - button "Fit width" [pressed] [ref=e21] [cursor=pointer]
        - button "Reading direction" [ref=e22] [cursor=pointer]: LTR
      - generic "Chapter navigation" [ref=e23]:
        - button "Previous chapter" [ref=e24] [cursor=pointer]
        - button "Next chapter" [ref=e25] [cursor=pointer]
    - generic [ref=e26]:
      - generic [ref=e27]: Phase 4 Chapter 34
      - status [ref=e28]: saved locally
      - spinbutton "Current page" [active] [ref=e29]: "2"
      - button "Previous page" [ref=e30] [cursor=pointer]
      - button "Next page" [ref=e31] [cursor=pointer]
    - generic [ref=e32]: Left and right arrows change pages in paged modes. Space advances a page or spread. Left bracket opens the previous chapter and right bracket opens the next chapter. Shortcuts do not run while editing a form field.
    - img "Chapter 34, page 2" [ref=e36]
    - button "InfinityScan" [ref=e37] [cursor=pointer]
```

# Test source

```ts
  164 | 
  165 |   await page.route("**/auth/me", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Not authenticated" }) }));
  166 |   await page.route("**/auth/refresh", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Not authenticated" }) }));
  167 |   await page.route("**/reader/phase4-reader/chunks?*", async (route: Route) => {
  168 |     const url = new URL(route.request().url());
  169 |     const direction = url.searchParams.get("direction") === "previous" ? "previous" : "next";
  170 |     const requestedCursor = url.searchParams.get("cursor");
  171 |     const startId = url.searchParams.get("start_chapter_id");
  172 |     const key = `${direction}:${requestedCursor ?? `start:${startId}`}`;
  173 |     chunkRequestCounts.set(key, (chunkRequestCounts.get(key) ?? 0) + 1);
  174 |     if (direction === "next") {
  175 |       activeNext += 1;
  176 |       maxActiveNext = Math.max(maxActiveNext, activeNext);
  177 |     } else {
  178 |       activePrevious += 1;
  179 |       previousChunkRequests += 1;
  180 |       maxActivePrevious = Math.max(maxActivePrevious, activePrevious);
  181 |     }
  182 |     try {
  183 |       await new Promise((resolve) => setTimeout(resolve, 20));
  184 |       let indices: number[];
  185 |       if (startId) {
  186 |         const start = chapterIndexById.get(startId);
  187 |         if (start == null) {
  188 |           return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "Chapter not found" }) });
  189 |         }
  190 |         indices = direction === "next"
  191 |           ? [start, start + 1].filter((index) => index < chapters.length)
  192 |           : [start - 1, start].filter((index) => index >= 0);
  193 |       } else {
  194 |         const match = requestedCursor?.match(/^(next|previous)-(\d+)$/);
  195 |         if (!match) {
  196 |           return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "Invalid cursor" }) });
  197 |         }
  198 |         const boundary = Number(match[2]);
  199 |         indices = direction === "next"
  200 |           ? [boundary, boundary + 1].filter((index) => index < chapters.length)
  201 |           : [boundary - 1, boundary].filter((index) => index >= 0);
  202 |       }
  203 |       const first = indices[0];
  204 |       const last = indices.at(-1);
  205 |       const responseChapters = indices.map((index) => ({
  206 |         ...chapters[index],
  207 |         page_count: PAGES_PER_CHAPTER,
  208 |         previous_chapter_id: index > 0 ? chapters[index - 1].id : null,
  209 |         next_chapter_id: index < chapters.length - 1 ? chapters[index + 1].id : null,
  210 |       }));
  211 |       return route.fulfill({
  212 |         status: 200,
  213 |         contentType: "application/json",
  214 |         body: JSON.stringify({
  215 |           series: { id: uuid("52000000", 1), slug: "phase4-reader", title: "Phase 4 Reader" },
  216 |           chapters: responseChapters,
  217 |           next_cursor: last != null && last < chapters.length - 1 ? cursor("next", last + 1) : null,
  218 |           previous_cursor: first != null && first > 0 ? cursor("previous", first - 1) : null,
  219 |           has_more_next: last != null && last < chapters.length - 1,
  220 |           has_more_previous: first != null && first > 0,
  221 |         }),
  222 |       });
  223 |     } finally {
  224 |       if (direction === "next") activeNext -= 1;
  225 |       else activePrevious -= 1;
  226 |     }
  227 |   });
  228 |   await page.route(/\/media\/pages\/[^/?]+(?:\?.*)?$/, async (route) => {
  229 |     mediaRequests += 1;
  230 |     const url = new URL(route.request().url());
  231 |     if (url.searchParams.has("attempt")) mediaRetryRequests += 1;
  232 |     if (failNextMedia && !url.searchParams.has("attempt")) {
  233 |       failNextMedia = false;
  234 |       temporaryFailures += 1;
  235 |       await route.abort("failed");
  236 |       return;
  237 |     }
  238 |     await route.fulfill({ status: 200, contentType: "image/png", body: TINY_PNG });
  239 |   });
  240 | 
  241 |   await page.goto("/login");
  242 |   await page.goto(`/reader/phase4-reader/${chapters[0].id}`);
  243 |   await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  244 |   await assertReaderGates(page);
  245 | 
  246 |   const cdp = await page.context().newCDPSession(page);
  247 |   await cdp.send("Performance.enable");
  248 |   await cdp.send("HeapProfiler.collectGarbage");
  249 |   await scrollToChapter(page, chapters, 20, "next");
  250 |   await revealChapterSeparator(page);
  251 |   await cdp.send("HeapProfiler.collectGarbage");
  252 |   const baselineHeap = await heapUsed(cdp);
  253 |   let peakHeap = baselineHeap;
  254 | 
  255 |   await page.getByRole("button", { name: "Horizontal" }).click();
  256 |   await expect(page.locator('.reader[data-reading-mode="horizontal"]')).toBeVisible();
  257 |   await expect(page.locator('.reader-page-frame[data-image-state="loaded"]').first()).toBeVisible();
  258 |   await page.waitForTimeout(100);
  259 |   const pageInput = page.getByRole("spinbutton", { name: "Current page" });
  260 |   const currentPageValue = Number(await pageInput.inputValue());
  261 |   failNextMedia = true;
  262 |   await pageInput.fill(currentPageValue >= PAGES_PER_CHAPTER ? "1" : String(currentPageValue + 1));
  263 |   await expect.poll(() => temporaryFailures).toBe(1);
> 264 |   await expect.poll(() => mediaRetryRequests).toBeGreaterThanOrEqual(1);
      |                                               ^ Error: expect(received).toBeGreaterThanOrEqual(expected)
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
  357 |   expect(maxActiveNext).toBeLessThanOrEqual(1);
  358 |   expect(maxActivePrevious).toBeLessThanOrEqual(1);
  359 |   expect(temporaryFailures).toBe(1);
  360 |   expect(mediaRetryRequests).toBeGreaterThanOrEqual(1);
  361 |   expect(mediaRetryRequests).toBeLessThanOrEqual(3);
  362 |   expect(mediaRequests).toBeLessThan(CHAPTER_COUNT * PAGES_PER_CHAPTER);
  363 |   console.log(JSON.stringify({
  364 |     phase4Network: {
```