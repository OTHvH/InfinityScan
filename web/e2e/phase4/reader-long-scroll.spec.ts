import { expect, test, type CDPSession, type Page, type Route } from "@playwright/test";

const CHAPTER_COUNT = 200;
const PAGES_PER_CHAPTER = 10;
const MAX_RETAINED_CHAPTERS = 9;
const MAX_RETAINED_PAGES = 250;
// Allows one outgoing and one incoming Virtuoso range during fast recycling.
const MAX_RENDERED_PAGE_ELEMENTS = 64;
const MAX_RENDERED_SEPARATORS = 8;
const HEAP_GROWTH_LIMIT_BYTES = 64 * 1024 * 1024;
const observedBounds = {
  retainedChapters: 0,
  retainedPages: 0,
  renderedPages: 0,
};
const TINY_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

interface FixtureChapter {
  id: string;
  number: string;
  title: string;
  pages: Array<{
    id: string;
    page_number: number;
    media_path: string;
    width: number;
    height: number;
    aspect_ratio: number;
  }>;
}

interface ReaderRequestEvent {
  phase: "start" | "settle";
  pageSessionId: string;
  controllerId: number;
  generation: number;
  requestId: number;
  direction: "next" | "previous";
  cursor: string;
  reason: string;
  outcome?: "succeeded" | "failed" | "cancelled";
  retainedChapterIds: string[];
  evictedChapterIds: string[];
  cursorChapterIds: string[];
}

function uuid(prefix: string, value: number): string {
  return `${prefix}-0000-0000-0000-${String(value).padStart(12, "0")}`;
}

function buildChapters(): FixtureChapter[] {
  return Array.from({ length: CHAPTER_COUNT }, (_, index) => {
    const base = index + 1 + Math.floor(index / 20);
    const number = index % 13 === 4 ? `${base}.5` : String(base);
    return {
      id: uuid("50000000", index + 1),
      number,
      title: `Phase 4 Chapter ${number}`,
      pages: Array.from({ length: PAGES_PER_CHAPTER }, (__, pageIndex) => {
        const id = uuid("51000000", index * PAGES_PER_CHAPTER + pageIndex + 1);
        return {
          id,
          page_number: pageIndex + 1,
          media_path: `/media/pages/${id}`,
          width: 16,
          height: 24,
          aspect_ratio: 2 / 3,
        };
      }),
    };
  });
}

function cursor(direction: "next" | "previous", index: number): string {
  return `${direction}-${index}`;
}

async function heapUsed(cdp: CDPSession): Promise<number> {
  const result = await cdp.send("Performance.getMetrics");
  const metric = result.metrics.find((item) => item.name === "JSHeapUsedSize");
  if (!metric || !Number.isFinite(metric.value)) throw new Error("Mandatory JSHeapUsedSize measurement unavailable");
  return metric.value;
}

async function diagnostics(page: Page): Promise<Record<string, number | string>> {
  const locator = page.getByTestId("reader-diagnostics");
  await expect(locator).toBeAttached();
  return locator.evaluate((element) => ({
    visibleChapterId: element.getAttribute("data-visible-chapter-id") ?? "",
    visiblePageId: element.getAttribute("data-visible-page-id") ?? "",
    visiblePageNumber: Number(element.getAttribute("data-visible-page-number")),
    retainedChapters: Number(element.getAttribute("data-retained-chapters")),
    retainedPages: Number(element.getAttribute("data-retained-pages")),
    activeNext: Number(element.getAttribute("data-active-next-requests")),
    activePrevious: Number(element.getAttribute("data-active-previous-requests")),
    hasMoreNext: Number(element.getAttribute("data-has-more-next")),
    hasMorePrevious: Number(element.getAttribute("data-has-more-previous")),
  }));
}

async function nextPaint(page: Page): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
}

async function waitForReaderIdle(page: Page): Promise<void> {
  await expect.poll(async () => {
    const state = await diagnostics(page);
    return Number(state.activeNext) + Number(state.activePrevious);
  }).toBe(0);
}

async function waitForReaderSettled(page: Page): Promise<void> {
  await waitForReaderIdle(page);
  await page.locator("[data-virtuoso-scroller]").evaluate(async (element) => {
    let previousSignature = "";
    let stableFrames = 0;
    for (let frame = 0; frame < 240; frame += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      const diagnostics = document.querySelector('[data-testid="reader-diagnostics"]');
      const activeRequests = Number(diagnostics?.getAttribute("data-active-next-requests"))
        + Number(diagnostics?.getAttribute("data-active-previous-requests"));
      const scrollerRect = element.getBoundingClientRect();
      const pendingImages = [...document.querySelectorAll<HTMLElement>(
        '.reader-page-frame[data-image-state="waiting"], .reader-page-frame[data-image-state="loading"], .reader-page-frame[data-image-state="retrying"]',
      )].filter((frame) => {
        const rect = frame.getBoundingClientRect();
        return rect.bottom > scrollerRect.top && rect.top < scrollerRect.bottom;
      }).length;
      const scrollSeekPlaceholders = element.querySelectorAll(".page-placeholder").length;
      const signature = `${element.scrollHeight}:${element.clientHeight}:${element.querySelectorAll("[data-item-index]").length}`;
      stableFrames = activeRequests === 0 && pendingImages === 0 && scrollSeekPlaceholders === 0 && signature === previousSignature
        ? stableFrames + 1
        : 0;
      if (stableFrames >= 3) return;
      previousSignature = signature;
    }
    throw new Error("Reader layout did not reach a stable image and geometry state");
  });
  await waitForReaderIdle(page);
}

async function assertReaderGates(page: Page): Promise<void> {
  const state = await diagnostics(page);
  expect(state.retainedChapters).toBeLessThanOrEqual(MAX_RETAINED_CHAPTERS);
  expect(state.retainedPages).toBeLessThanOrEqual(MAX_RETAINED_PAGES);
  expect(state.activeNext).toBeLessThanOrEqual(1);
  expect(state.activePrevious).toBeLessThanOrEqual(1);

  const pageIds = await page.locator(".reader-page-frame[data-page-id]").evaluateAll((elements) =>
    elements.map((element) => element.getAttribute("data-page-id") ?? ""),
  );
  const separatorIds = await page.locator(".chapter-separator[data-chapter-id]").evaluateAll((elements) =>
    elements.map((element) => element.getAttribute("data-chapter-id") ?? ""),
  );
  observedBounds.retainedChapters = Math.max(observedBounds.retainedChapters, Number(state.retainedChapters));
  observedBounds.retainedPages = Math.max(observedBounds.retainedPages, Number(state.retainedPages));
  observedBounds.renderedPages = Math.max(observedBounds.renderedPages, pageIds.length);
  expect(pageIds.length).toBeLessThanOrEqual(MAX_RENDERED_PAGE_ELEMENTS);
  expect(separatorIds.length).toBeLessThanOrEqual(MAX_RENDERED_SEPARATORS);
  expect(new Set(pageIds).size).toBe(pageIds.length);
  expect(new Set(separatorIds).size).toBe(separatorIds.length);
}

async function revealChapterSeparator(page: Page): Promise<void> {
  const scroller = page.locator("[data-virtuoso-scroller]");
  for (let step = 0; step <= 120; step += 1) {
    await scroller.evaluate((element, ratio) => {
      const maximum = Math.max(0, element.scrollHeight - element.clientHeight);
      element.scrollTo({ top: maximum * ratio, behavior: "instant" });
    }, step / 120);
    await nextPaint(page);
    if (await page.locator(".chapter-separator[data-chapter-id]").count()) return;
  }
  throw new Error("No continuous-reader chapter separator appeared in the retained virtual range");
}

async function scrollToChapter(
  page: Page,
  chapters: FixtureChapter[],
  targetIndex: number,
  direction: "next" | "previous",
  onStep?: () => Promise<void>,
): Promise<number> {
  const scroller = page.locator("[data-virtuoso-scroller]");
  await expect(scroller).toBeVisible();
  let furthestIndex = direction === "next" ? -1 : chapters.length;
  for (let step = 0; step < 240; step += 1) {
    await waitForReaderSettled(page);
    const state = await diagnostics(page);
    const index = chapters.findIndex((chapter) => chapter.id === state.visibleChapterId);
    furthestIndex = direction === "next" ? Math.max(furthestIndex, index) : Math.min(furthestIndex, index);
    if ((direction === "next" && index >= targetIndex) || (direction === "previous" && index <= targetIndex)) return furthestIndex;
    const targetFrames = page.locator(`.reader-page-frame[data-chapter-id="${chapters[targetIndex].id}"]`);
    if (await targetFrames.count()) {
      const targetFrame = direction === "next" ? targetFrames.last() : targetFrames.first();
      await targetFrame.evaluate((element, scrollDirection) => {
        const scrollElement = element.closest<HTMLElement>("[data-virtuoso-scroller]");
        if (!scrollElement) throw new Error("Virtuoso scroller is unavailable");
        const targetRect = element.getBoundingClientRect();
        const scrollerRect = scrollElement.getBoundingClientRect();
        const remaining = scrollDirection === "next"
          ? targetRect.bottom - scrollerRect.bottom
          : targetRect.top - scrollerRect.top;
        const maximumStep = scrollElement.clientHeight * 2;
        scrollElement.scrollBy({
          top: Math.max(-maximumStep, Math.min(maximumStep, remaining)),
          behavior: "instant",
        });
      }, direction);
    } else {
      await scroller.evaluate((element, scrollDirection) => {
        element.scrollTo({ top: scrollDirection === "next" ? element.scrollHeight : 0, behavior: "instant" });
      }, direction);
    }
    await nextPaint(page);
    await waitForReaderSettled(page);
    const nextState = await diagnostics(page);
    const nextIndex = chapters.findIndex((chapter) => chapter.id === nextState.visibleChapterId);
    furthestIndex = direction === "next" ? Math.max(furthestIndex, nextIndex) : Math.min(furthestIndex, nextIndex);
    await assertReaderGates(page);
    await onStep?.();
  }
  const failureState = await scroller.evaluate((element) => ({
    scrollTop: element.scrollTop,
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
    placeholders: element.querySelectorAll(".page-placeholder").length,
    renderedChapterIds: [...element.querySelectorAll<HTMLElement>("[data-chapter-id]")]
      .map((item) => item.dataset.chapterId),
  }));
  throw new Error(`Did not reach chapter index ${targetIndex} while scrolling ${direction}: ${JSON.stringify(failureState)}`);
}

test("Phase 4 long scroll remains bounded and resumable", async ({ page, context, browserName }) => {
  expect(browserName).toBe("chromium");
  const chapters = buildChapters();
  const chapterIndexById = new Map(chapters.map((chapter, index) => [chapter.id, index]));
  const chunkRequestCounts = new Map<string, number>();
  const requestEvents: ReaderRequestEvent[] = [];
  let activeNext = 0;
  let activePrevious = 0;
  let maxActiveNext = 0;
  let maxActivePrevious = 0;
  let nextChunkRequests = 0;
  let previousChunkRequests = 0;
  let mediaRequests = 0;
  let mediaRetryRequests = 0;
  let failMediaPageId: string | null = chapters[0].pages[0].id;
  let temporaryFailures = 0;

  await context.clearCookies();
  await page.exposeFunction("recordReaderRequest", (event: ReaderRequestEvent) => {
    requestEvents.push(event);
  });
  await page.addInitScript(() => {
    window.addEventListener("infinityscan:reader-request", (event) => {
      const recorder = (window as typeof window & { recordReaderRequest: (detail: unknown) => Promise<void> }).recordReaderRequest;
      void recorder((event as CustomEvent).detail);
    });
  });

  await page.route("**/api/auth/me", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Not authenticated" }) }));
  await page.route("**/api/auth/refresh", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Not authenticated" }) }));
  await page.route("**/api/reader/phase4-reader/chunks?*", async (route: Route) => {
    const url = new URL(route.request().url());
    const direction = url.searchParams.get("direction") === "previous" ? "previous" : "next";
    const requestedCursor = url.searchParams.get("cursor");
    const startId = url.searchParams.get("start_chapter_id");
    const key = `${direction}:${requestedCursor ?? `start:${startId}`}`;
    chunkRequestCounts.set(key, (chunkRequestCounts.get(key) ?? 0) + 1);
    if (direction === "next") {
      activeNext += 1;
      nextChunkRequests += 1;
      maxActiveNext = Math.max(maxActiveNext, activeNext);
    } else {
      activePrevious += 1;
      previousChunkRequests += 1;
      maxActivePrevious = Math.max(maxActivePrevious, activePrevious);
    }
    try {
      let indices: number[];
      if (startId) {
        const start = chapterIndexById.get(startId);
        if (start == null) {
          return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "Chapter not found" }) });
        }
        indices = direction === "next"
          ? [start, start + 1].filter((index) => index < chapters.length)
          : [start - 1, start].filter((index) => index >= 0);
      } else {
        const match = requestedCursor?.match(/^(next|previous)-(\d+)$/);
        if (!match) {
          return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "Invalid cursor" }) });
        }
        const boundary = Number(match[2]);
        indices = direction === "next"
          ? [boundary, boundary + 1].filter((index) => index < chapters.length)
          : [boundary - 1, boundary].filter((index) => index >= 0);
      }
      const first = indices[0];
      const last = indices.at(-1);
      const responseChapters = indices.map((index) => ({
        ...chapters[index],
        page_count: PAGES_PER_CHAPTER,
        previous_chapter_id: index > 0 ? chapters[index - 1].id : null,
        next_chapter_id: index < chapters.length - 1 ? chapters[index + 1].id : null,
      }));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          series: { id: uuid("52000000", 1), slug: "phase4-reader", title: "Phase 4 Reader" },
          chapters: responseChapters,
          chapter_boundaries: indices.map((index) => ({
            chapter_id: chapters[index].id,
            next_cursor: index < chapters.length - 1 ? cursor("next", index + 1) : null,
            previous_cursor: index > 0 ? cursor("previous", index - 1) : null,
            has_more_next: index < chapters.length - 1,
            has_more_previous: index > 0,
          })),
          next_cursor: last != null && last < chapters.length - 1 ? cursor("next", last + 1) : null,
          previous_cursor: first != null && first > 0 ? cursor("previous", first - 1) : null,
          has_more_next: last != null && last < chapters.length - 1,
          has_more_previous: first != null && first > 0,
        }),
      });
    } finally {
      if (direction === "next") activeNext -= 1;
      else activePrevious -= 1;
    }
  });
  await page.route(/\/api\/media\/pages\/[^/?]+(?:\?.*)?$/, async (route) => {
    mediaRequests += 1;
    const url = new URL(route.request().url());
    if (url.searchParams.has("attempt")) mediaRetryRequests += 1;
    const pageId = url.pathname.split("/").at(-1) ?? "";
    if (failMediaPageId === pageId) {
      if (url.searchParams.has("attempt")) {
        failMediaPageId = null;
      } else {
        if (temporaryFailures === 0) temporaryFailures = 1;
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "temporary fixture failure" }) });
        return;
      }
    }
    await route.fulfill({ status: 200, contentType: "image/png", body: TINY_PNG });
  });

  await page.goto("/login");
  await page.evaluate((slug) => {
    localStorage.clear();
    sessionStorage.clear();
    localStorage.setItem("infinityscan_reader_debug", "1");
    localStorage.setItem(`infinityscan_reader_preferences_${slug}`, JSON.stringify({
      mode: "continuous",
      spreadMode: "single",
      zoom: 100,
      fitWidth: true,
      direction: "ltr",
      firstPageAlone: false,
    }));
  }, "phase4-reader");
  await page.goto(`/reader/phase4-reader/${chapters[0].id}`);
  await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  await expect(page.getByTestId("reader-diagnostics")).toHaveAttribute("data-visible-chapter-id", chapters[0].id);
  await waitForReaderSettled(page);
  await expect.poll(() => temporaryFailures).toBe(1);
  await expect.poll(() => mediaRetryRequests).toBe(1);
  await assertReaderGates(page);

  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  await cdp.send("HeapProfiler.collectGarbage");
  await scrollToChapter(page, chapters, 20, "next");
  await revealChapterSeparator(page);
  await cdp.send("HeapProfiler.collectGarbage");
  const baselineHeap = await heapUsed(cdp);
  let peakHeap = baselineHeap;

  await page.getByRole("button", { name: "Horizontal" }).click();
  await expect(page.locator('.reader[data-reading-mode="horizontal"]')).toBeVisible();
  await expect(page.locator('.reader-page-frame[data-image-state="loaded"]').first()).toBeVisible();
  const pageInput = page.getByRole("spinbutton", { name: "Current page" });
  const currentPageValue = Number(await pageInput.inputValue());
  const currentState = await diagnostics(page);
  const currentChapterIndex = chapterIndexById.get(String(currentState.visibleChapterId)) ?? 0;
  const targetPageNumber = currentPageValue >= PAGES_PER_CHAPTER ? 1 : currentPageValue + 1;
  const targetPageId = chapters[currentChapterIndex].pages[targetPageNumber - 1].id;
  await pageInput.fill(String(targetPageNumber));
  const targetFrame = page.locator(`.reader-page-frame[data-page-id="${targetPageId}"]`);
  await expect(targetFrame).toHaveAttribute("data-image-state", "loaded");
  await page.getByRole("button", { name: "Continuous" }).click();
  await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();

  const furthestChapterIndex = await scrollToChapter(page, chapters, 105, "next", async () => {
    peakHeap = Math.max(peakHeap, await heapUsed(cdp));
  });
  await waitForReaderSettled(page);
  await assertReaderGates(page);
  const atHundred = await diagnostics(page);
  const visibleChapterId = String(atHundred.visibleChapterId);
  const visiblePageNumber = Number(atHundred.visiblePageNumber);
  expect(furthestChapterIndex).toBeGreaterThanOrEqual(105);
  await expect(page).toHaveURL(new RegExp(`/reader/phase4-reader/${visibleChapterId}$`));
  await expect.poll(async () => page.evaluate(({ slug, chapterId, pageNumber }) => {
    const raw = localStorage.getItem(`infinityscan_progress_${slug}_${chapterId}`);
    if (!raw) return false;
    const saved = JSON.parse(raw) as { chapterId?: string; page?: number };
    return saved.chapterId === chapterId && saved.page === pageNumber;
  }, { slug: "phase4-reader", chapterId: visibleChapterId, pageNumber: visiblePageNumber })).toBe(true);
  const savedProgress = await page.evaluate(({ slug, chapterId }) => {
    const raw = localStorage.getItem(`infinityscan_progress_${slug}_${chapterId}`);
    return raw ? JSON.parse(raw) : null;
  }, { slug: "phase4-reader", chapterId: visibleChapterId });
  expect(savedProgress).toMatchObject({ chapterId: visibleChapterId, page: visiblePageNumber });

  await cdp.send("HeapProfiler.collectGarbage");
  const postEvictionHeap = await heapUsed(cdp);
  peakHeap = Math.max(peakHeap, postEvictionHeap);
  console.log(JSON.stringify({
    phase4MemoryBytes: { baseline: baselineHeap, peak: peakHeap, postEviction: postEvictionHeap },
    phase4MemoryMiB: {
      baseline: Number((baselineHeap / 1024 / 1024).toFixed(2)),
      peak: Number((peakHeap / 1024 / 1024).toFixed(2)),
      postEviction: Number((postEvictionHeap / 1024 / 1024).toFixed(2)),
    },
    heapGrowthLimitBytes: HEAP_GROWTH_LIMIT_BYTES,
  }));
  expect(postEvictionHeap).toBeLessThanOrEqual(baselineHeap + HEAP_GROWTH_LIMIT_BYTES);

  const mediaRequestsBeforeResume = mediaRequests;
  await page.goBack();
  await expect(page).toHaveURL(/\/login$/);
  await page.goForward();
  await expect(page.getByTestId("reader-diagnostics")).toBeAttached();
  await waitForReaderSettled(page);
  await page.reload();
  await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(visibleChapterId);
  await waitForReaderSettled(page);
  const resumed = await diagnostics(page);
  expect(Math.abs(Number(resumed.visiblePageNumber) - visiblePageNumber)).toBeLessThanOrEqual(2);

  const previousBefore = previousChunkRequests;
  const currentIndex = chapterIndexById.get(visibleChapterId) ?? 105;
  await scrollToChapter(page, chapters, Math.max(0, currentIndex - 6), "previous");
  expect(previousChunkRequests).toBeGreaterThan(previousBefore);
  await waitForReaderSettled(page);
  const afterBackward = await diagnostics(page);
  const transitionChapterId = String(afterBackward.visibleChapterId);
  await page.getByRole("button", { name: "Horizontal" }).click();
  await expect(page.locator('.reader[data-reading-mode="horizontal"]')).toBeVisible();
  await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(transitionChapterId);
  await page.getByRole("button", { name: "Continuous" }).click();
  await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(transitionChapterId);

  const exhaustedAt = await scrollToChapter(page, chapters, CHAPTER_COUNT - 1, "next");
  expect(exhaustedAt).toBe(CHAPTER_COUNT - 1);
  const finalChapterId = chapters.at(-1)?.id ?? "";
  await page.getByRole("button", { name: "Horizontal" }).click();
  await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(finalChapterId);
  await page.getByRole("button", { name: "Continuous" }).click();
  await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(finalChapterId);
  await expect(page).toHaveURL(new RegExp(`/reader/phase4-reader/${finalChapterId}$`));
  await expect.poll(async () => {
    const state = await diagnostics(page);
    return Number(state.activeNext) + Number(state.activePrevious);
  }).toBe(0);
  await expect(page.getByTestId("reader-diagnostics")).toHaveAttribute("data-has-more-next", "0");
  await waitForReaderSettled(page);
  const exhaustionCounts = new Map(chunkRequestCounts);
  const requestsAtExhaustion = [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0);
  const nextRequestsAtExhaustion = nextChunkRequests;
  const finalScroller = page.locator("[data-virtuoso-scroller]");
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await finalScroller.evaluate((element) => element.scrollTo({ top: element.scrollHeight, behavior: "instant" }));
    await nextPaint(page);
  }
  await waitForReaderSettled(page);
  const requestsAfterExhaustion = [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0);
  const exhaustionRequests = [...chunkRequestCounts.entries()]
    .filter(([key, count]) => count > (exhaustionCounts.get(key) ?? 0));
  console.log(JSON.stringify({
    phase4ExhaustionRequests: exhaustionRequests,
  }));
  expect(nextChunkRequests).toBe(nextRequestsAtExhaustion);
  expect(exhaustionRequests.every(([key, count]) => key.startsWith("previous:") && count - (exhaustionCounts.get(key) ?? 0) <= 1)).toBe(true);
  await expect.poll(() => requestEvents.filter((event) => event.phase === "start").length).toBe(requestsAfterExhaustion);

  const activeByController = new Map<string, number>();
  let controllerMaxActiveNext = 0;
  let controllerMaxActivePrevious = 0;
  let duplicateTriggerRequests = 0;
  for (const event of requestEvents) {
    const key = `${event.pageSessionId}:${event.controllerId}:${event.generation}:${event.direction}`;
    const active = activeByController.get(key) ?? 0;
    if (event.phase === "start") {
      if (active > 0) duplicateTriggerRequests += 1;
      const nextActive = active + 1;
      activeByController.set(key, nextActive);
      if (event.direction === "next") controllerMaxActiveNext = Math.max(controllerMaxActiveNext, nextActive);
      else controllerMaxActivePrevious = Math.max(controllerMaxActivePrevious, nextActive);
    } else {
      activeByController.set(key, Math.max(0, active - 1));
    }
  }
  const starts = requestEvents.filter((event) => event.phase === "start");
  const controllersByPageSession = new Map<string, Set<number>>();
  for (const event of starts) {
    const controllers = controllersByPageSession.get(event.pageSessionId) ?? new Set<number>();
    controllers.add(event.controllerId);
    controllersByPageSession.set(event.pageSessionId, controllers);
  }
  const initialSessions = new Set(starts
    .filter((event) => event.reason === "initial" && event.cursor.startsWith("start:"))
    .map((event) => `${event.pageSessionId}:${event.controllerId}:${event.generation}`));
  const resumeSessions = new Set(starts
    .filter((event) => event.reason === "resume" && event.cursor.startsWith("start:"))
    .map((event) => `${event.pageSessionId}:${event.controllerId}:${event.generation}`));
  const settlesByRequest = new Map(requestEvents
    .filter((event) => event.phase === "settle")
    .map((event) => [`${event.pageSessionId}:${event.controllerId}:${event.generation}:${event.requestId}`, event]));
  const groupedStarts = new Map<string, ReaderRequestEvent[]>();
  for (const event of starts) {
    const key = `${event.direction}:${event.cursor}`;
    groupedStarts.set(key, [...(groupedStarts.get(key) ?? []), event]);
  }
  const repeatedCursorClassifications = [...groupedStarts.entries()]
    .filter(([, events]) => events.length > 1)
    .map(([key, events]) => {
      const repetitions = events.slice(1).map((event, index) => {
        const priorEvents = events.slice(0, index + 1);
        const priorInController = priorEvents.filter((prior) =>
          prior.pageSessionId === event.pageSessionId
          && prior.controllerId === event.controllerId
          && prior.generation === event.generation,
        );
        const priorInControllerLifecycle = priorEvents.filter((prior) =>
          prior.pageSessionId === event.pageSessionId && prior.controllerId === event.controllerId,
        );
        const priorFailure = priorInControllerLifecycle.some((prior) =>
          settlesByRequest.get(`${prior.pageSessionId}:${prior.controllerId}:${prior.generation}:${prior.requestId}`)?.outcome === "failed",
        );
        const sessionKey = `${event.pageSessionId}:${event.controllerId}:${event.generation}`;
        const reloadsMissingCursorChapter = event.cursorChapterIds.some((chapterId) =>
          event.evictedChapterIds.includes(chapterId) && !event.retainedChapterIds.includes(chapterId),
        );
        const classification = event.reason === "evictionReload" && reloadsMissingCursorChapter
          ? "evictionReload"
          : event.reason === "retry" && priorFailure
            ? "retryAfterFailure"
            : priorInController.length === 0 && resumeSessions.has(sessionKey)
              ? "resumeSession"
              : priorInController.length === 0 && initialSessions.has(sessionKey)
                ? "initialSession"
                : "unclassified";
        return { event, classification, allowed: classification !== "unclassified" };
      });
      const allowed = repetitions.every((entry) => entry.allowed);
      return {
        cursor: key.slice(key.indexOf(":") + 1),
        direction: events[0].direction,
        requestReasons: events.map((event) => event.reason),
        classifications: repetitions.map((entry) => entry.classification),
        allowed,
        states: events.map((event) => ({
          pageSessionId: event.pageSessionId,
          controllerId: event.controllerId,
          generation: event.generation,
          retained: event.retainedChapterIds,
          evicted: event.evictedChapterIds,
          cursorChapters: event.cursorChapterIds,
        })),
      };
    });
  console.log(JSON.stringify({ phase4CursorClassifications: repeatedCursorClassifications }));
  expect(maxActiveNext).toBeLessThanOrEqual(1);
  expect(maxActivePrevious).toBeLessThanOrEqual(1);
  expect(controllerMaxActiveNext).toBeLessThanOrEqual(1);
  expect(controllerMaxActivePrevious).toBeLessThanOrEqual(1);
  expect([...controllersByPageSession.values()].filter((controllers) => controllers.size > 1)).toEqual([]);
  const unclassifiedRepeatedCursors = repeatedCursorClassifications.filter((entry) => !entry.allowed);
  const runawayRepeatedRequests = repeatedCursorClassifications.filter((entry) => entry.requestReasons.length > 10);
  expect(unclassifiedRepeatedCursors).toEqual([]);
  expect(duplicateTriggerRequests).toBe(0);
  expect(runawayRepeatedRequests).toEqual([]);
  expect(temporaryFailures).toBe(1);
  expect(mediaRetryRequests).toBeGreaterThanOrEqual(1);
  expect(mediaRetryRequests).toBeLessThanOrEqual(3);
  const mediaRequestsAfterResume = mediaRequests - mediaRequestsBeforeResume;
  expect(mediaRequestsBeforeResume).toBeLessThan(CHAPTER_COUNT * PAGES_PER_CHAPTER);
  expect(mediaRequestsAfterResume).toBeLessThan(CHAPTER_COUNT * PAGES_PER_CHAPTER);
  expect(mediaRequests).toBeLessThan(CHAPTER_COUNT * PAGES_PER_CHAPTER * 2);
  console.log(JSON.stringify({
    phase4Network: {
      chunkRequests: [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0),
      repeatedChunkRequests: [...chunkRequestCounts.entries()].filter(([, count]) => count > 1),
      mediaRequests,
      mediaRequestsBeforeResume,
      mediaRequestsAfterResume,
      mediaRetryRequests,
      maxActiveNext,
      maxActivePrevious,
      controllerMaxActiveNext,
      controllerMaxActivePrevious,
      unclassifiedRepeatedCursors: unclassifiedRepeatedCursors.length,
      duplicateTriggerRequests,
      requestsAfterCursorExhaustion: requestsAfterExhaustion - requestsAtExhaustion,
      runawayRepeatedRequests: runawayRepeatedRequests.length,
      retainedChapterMaximum: observedBounds.retainedChapters,
      retainedPageMaximum: observedBounds.retainedPages,
      renderedPageMaximum: observedBounds.renderedPages,
    },
  }));
  expect([...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0)).toBeLessThan(180);
  await assertReaderGates(page);
});
