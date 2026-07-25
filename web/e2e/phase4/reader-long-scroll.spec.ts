import { expect, test, type CDPSession, type Page, type Route } from "@playwright/test";

const CHAPTER_COUNT = 200;
const PAGES_PER_CHAPTER = 10;
const MAX_RETAINED_CHAPTERS = 9;
const MAX_RETAINED_PAGES = 250;
// Allows one outgoing and one incoming Virtuoso range during fast recycling.
const MAX_RENDERED_PAGE_ELEMENTS = 64;
const MAX_RENDERED_SEPARATORS = 8;
const HEAP_GROWTH_LIMIT_BYTES = 64 * 1024 * 1024;
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
  controllerId: number;
  generation: number;
  requestId: number;
  direction: "next" | "previous";
  cursor: string;
  reason: string;
  outcome?: "succeeded" | "failed" | "cancelled";
  retainedChapterIds: string[];
  evictedChapterIds: string[];
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
  for (let step = 0; step < 180; step += 1) {
    const state = await diagnostics(page);
    const index = chapters.findIndex((chapter) => chapter.id === state.visibleChapterId);
    furthestIndex = direction === "next" ? Math.max(furthestIndex, index) : Math.min(furthestIndex, index);
    if ((direction === "next" && index >= targetIndex) || (direction === "previous" && index <= targetIndex)) return furthestIndex;
    await scroller.evaluate((element, scrollDirection) => {
      element.scrollTo({ top: scrollDirection === "next" ? element.scrollHeight : 0, behavior: "instant" });
    }, direction);
    await expect.poll(async () => {
      const nextState = await diagnostics(page);
      const nextIndex = chapters.findIndex((chapter) => chapter.id === nextState.visibleChapterId);
      return Number(nextState.activeNext) + Number(nextState.activePrevious) === 0
        && (nextIndex !== index || nextIndex === targetIndex);
    }).toBe(true);
    await assertReaderGates(page);
    await onStep?.();
  }
  throw new Error(`Did not reach chapter index ${targetIndex} while scrolling ${direction}`);
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
  let previousChunkRequests = 0;
  let mediaRequests = 0;
  let mediaRetryRequests = 0;
  let failMediaPageId: string | null = null;
  let temporaryFailures = 0;

  await context.clearCookies();
  await page.exposeFunction("recordReaderRequest", (event: ReaderRequestEvent) => {
    requestEvents.push(event);
  });
  await page.addInitScript((slug) => {
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
    window.addEventListener("infinityscan:reader-request", (event) => {
      const recorder = (window as typeof window & { recordReaderRequest: (detail: unknown) => Promise<void> }).recordReaderRequest;
      void recorder((event as CustomEvent).detail);
    });
  }, "phase4-reader");

  await page.route("**/auth/me", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Not authenticated" }) }));
  await page.route("**/auth/refresh", (route) => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Not authenticated" }) }));
  await page.route("**/reader/phase4-reader/chunks?*", async (route: Route) => {
    const url = new URL(route.request().url());
    const direction = url.searchParams.get("direction") === "previous" ? "previous" : "next";
    const requestedCursor = url.searchParams.get("cursor");
    const startId = url.searchParams.get("start_chapter_id");
    const key = `${direction}:${requestedCursor ?? `start:${startId}`}`;
    chunkRequestCounts.set(key, (chunkRequestCounts.get(key) ?? 0) + 1);
    if (direction === "next") {
      activeNext += 1;
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
  await page.route(/\/media\/pages\/[^/?]+(?:\?.*)?$/, async (route) => {
    mediaRequests += 1;
    const url = new URL(route.request().url());
    if (url.searchParams.has("attempt")) mediaRetryRequests += 1;
    const pageId = url.pathname.split("/").at(-1) ?? "";
    if (failMediaPageId === pageId && !url.searchParams.has("attempt")) {
      failMediaPageId = null;
      temporaryFailures += 1;
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "temporary fixture failure" }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "image/png", body: TINY_PNG });
  });

  await page.goto("/login");
  await page.goto(`/reader/phase4-reader/${chapters[0].id}`);
  await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  await expect(page.getByTestId("reader-diagnostics")).toHaveAttribute("data-visible-chapter-id", chapters[0].id);
  await waitForReaderIdle(page);
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
  failMediaPageId = targetPageId;
  await pageInput.fill(String(targetPageNumber));
  const targetFrame = page.locator(`.reader-page-frame[data-page-id="${targetPageId}"]`);
  await expect(targetFrame).toHaveAttribute("data-image-state", "retrying");
  await expect.poll(() => temporaryFailures).toBe(1);
  await expect.poll(() => mediaRetryRequests).toBe(1);
  await expect(targetFrame).toHaveAttribute("data-image-state", "loaded");
  await page.getByRole("button", { name: "Continuous" }).click();
  await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();

  const furthestChapterIndex = await scrollToChapter(page, chapters, 105, "next", async () => {
    peakHeap = Math.max(peakHeap, await heapUsed(cdp));
  });
  await expect.poll(async () => page.evaluate(({ slug, chapterId }) => {
    const raw = localStorage.getItem(`infinityscan_progress_${slug}_${chapterId}`);
    return raw ? JSON.parse(raw).chapterId : null;
  }, { slug: "phase4-reader", chapterId: String((await diagnostics(page)).visibleChapterId) })).not.toBeNull();
  await waitForReaderIdle(page);
  await assertReaderGates(page);
  const atHundred = await diagnostics(page);
  const visibleChapterId = String(atHundred.visibleChapterId);
  const visiblePageNumber = Number(atHundred.visiblePageNumber);
  expect(furthestChapterIndex).toBeGreaterThanOrEqual(105);
  await expect(page).toHaveURL(new RegExp(`/reader/phase4-reader/${visibleChapterId}$`));
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

  await page.goBack();
  await expect(page).toHaveURL(/\/login$/);
  await page.goForward();
  await expect(page.getByTestId("reader-diagnostics")).toBeAttached();
  await waitForReaderIdle(page);
  await page.reload();
  await expect(page.locator('.reader[data-reading-mode="continuous"]')).toBeVisible();
  await expect.poll(async () => String((await diagnostics(page)).visibleChapterId)).toBe(visibleChapterId);
  await waitForReaderIdle(page);
  const resumed = await diagnostics(page);
  expect(Math.abs(Number(resumed.visiblePageNumber) - visiblePageNumber)).toBeLessThanOrEqual(2);

  const previousBefore = previousChunkRequests;
  const currentIndex = chapterIndexById.get(visibleChapterId) ?? 105;
  await scrollToChapter(page, chapters, Math.max(0, currentIndex - 6), "previous");
  expect(previousChunkRequests).toBeGreaterThan(previousBefore);
  await waitForReaderIdle(page);
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
  await waitForReaderIdle(page);
  const exhaustionCounts = new Map(chunkRequestCounts);
  const requestsAtExhaustion = [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0);
  const finalScroller = page.locator("[data-virtuoso-scroller]");
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await finalScroller.evaluate((element) => element.scrollTo({ top: element.scrollHeight, behavior: "instant" }));
    await nextPaint(page);
  }
  const requestsAfterExhaustion = [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0);
  console.log(JSON.stringify({
    phase4ExhaustionRequests: [...chunkRequestCounts.entries()].filter(([key, count]) => count > (exhaustionCounts.get(key) ?? 0)),
  }));
  expect(requestsAfterExhaustion).toBe(requestsAtExhaustion);

  const activeByController = new Map<string, number>();
  let controllerMaxActiveNext = 0;
  let controllerMaxActivePrevious = 0;
  let duplicateTriggerRequests = 0;
  for (const event of requestEvents) {
    const key = `${event.controllerId}:${event.generation}:${event.direction}`;
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
  const groupedStarts = new Map<string, ReaderRequestEvent[]>();
  for (const event of starts) {
    const key = `${event.direction}:${event.cursor}`;
    groupedStarts.set(key, [...(groupedStarts.get(key) ?? []), event]);
  }
  const repeatedCursorClassifications = [...groupedStarts.entries()]
    .filter(([, events]) => events.length > 1)
    .map(([key, events]) => {
      const repetitions = events.slice(1);
      const allowed = repetitions.every((event) => ["evictionReload", "retry", "resume"].includes(event.reason));
      return {
        cursor: key.slice(key.indexOf(":") + 1),
        direction: events[0].direction,
        requestReasons: events.map((event) => event.reason),
        allowed,
        states: events.map((event) => ({
          retained: event.retainedChapterIds,
          evicted: event.evictedChapterIds,
        })),
      };
    });
  console.log(JSON.stringify({ phase4CursorClassifications: repeatedCursorClassifications }));
  expect(controllerMaxActiveNext).toBeLessThanOrEqual(1);
  expect(controllerMaxActivePrevious).toBeLessThanOrEqual(1);
  expect(repeatedCursorClassifications.filter((entry) => !entry.allowed)).toEqual([]);
  expect(duplicateTriggerRequests).toBe(0);
  expect(repeatedCursorClassifications.filter((entry) => entry.requestReasons.length > 10)).toEqual([]);
  expect(temporaryFailures).toBe(1);
  expect(mediaRetryRequests).toBeGreaterThanOrEqual(1);
  expect(mediaRetryRequests).toBeLessThanOrEqual(3);
  expect(mediaRequests).toBeLessThan(CHAPTER_COUNT * PAGES_PER_CHAPTER);
  console.log(JSON.stringify({
    phase4Network: {
      chunkRequests: [...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0),
      repeatedChunkRequests: [...chunkRequestCounts.entries()].filter(([, count]) => count > 1),
      mediaRequests,
      mediaRetryRequests,
      maxActiveNext,
      maxActivePrevious,
      controllerMaxActiveNext,
      controllerMaxActivePrevious,
      unclassifiedRepeatedCursors: repeatedCursorClassifications.filter((entry) => !entry.allowed).length,
      duplicateTriggerRequests,
    },
  }));
  expect([...chunkRequestCounts.values()].reduce((sum, count) => sum + count, 0)).toBeLessThan(180);
  await assertReaderGates(page);
});
