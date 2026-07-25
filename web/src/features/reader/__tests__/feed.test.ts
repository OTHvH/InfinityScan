import { describe, expect, it, vi } from "vitest";
import {
  MAX_RETAINED_CHAPTERS,
  MAX_RETAINED_PAGES,
  ReaderFeedController,
  type ReaderChunkFetcher,
} from "../feed";
import type { ReaderChapter, ReaderChunkResponse } from "../types";

function chapter(id: string, number: string, pageIds = [`${id}-page`]): ReaderChapter {
  return {
    id,
    number,
    title: null,
    pageCount: pageIds.length,
    previousChapterId: null,
    nextChapterId: null,
    pages: pageIds.map((pageId, index) => ({
      id: pageId,
      pageNumber: index + 1,
      mediaPath: `/media/pages/${pageId}`,
      width: 100,
      height: 150,
      aspectRatio: 2 / 3,
    })),
  };
}

function response(
  chapters: ReaderChapter[],
  options: Partial<ReaderChunkResponse> = {},
): ReaderChunkResponse {
  const merged = {
    series: { id: "series-id", slug: "series", title: "Series" },
    chapters,
    nextCursor: null,
    previousCursor: null,
    hasMoreNext: false,
    hasMorePrevious: false,
    ...options,
  };
  return {
    ...merged,
    boundariesByChapterId: options.boundariesByChapterId ?? Object.fromEntries(chapters.map((item, index) => [
      item.id,
      {
        chapterId: item.id,
        previousCursor: index === 0 ? merged.previousCursor : `previous-${item.id}`,
        nextCursor: index === chapters.length - 1 ? merged.nextCursor : `next-${item.id}`,
        hasMorePrevious: index === 0 ? merged.hasMorePrevious : true,
        hasMoreNext: index === chapters.length - 1 ? merged.hasMoreNext : true,
      },
    ])),
  };
}

describe("ReaderFeedController", () => {
  it("loads the initial feed and flattens chapter separators and pages", async () => {
    const feed = new ReaderFeedController(async () => response([chapter("a", "1.5")], { nextCursor: "next-a", hasMoreNext: true }));

    await feed.loadInitial("series", "a");

    expect(feed.getState()).toMatchObject({
      initialChapterId: "a",
      visibleChapterId: "a",
      orderedChapterIds: ["a"],
      nextCursor: "next-a",
      hasMoreNext: true,
      loadingInitial: false,
    });
    expect(feed.getState().items.map((item) => item.kind)).toEqual(["chapter-separator", "page"]);
    expect(feed.getState().items[0]).toMatchObject({ chapterNumber: "1.5" });
  });

  it("appends and prepends chapters by UUID", async () => {
    const fetcher = vi.fn<ReaderChunkFetcher>()
      .mockResolvedValueOnce(response([chapter("b", "2")], { nextCursor: "next-b", hasMoreNext: true, previousCursor: "previous-b", hasMorePrevious: true }))
      .mockResolvedValueOnce(response([chapter("c", "3")], { nextCursor: null, hasMoreNext: false, previousCursor: "previous-c", hasMorePrevious: true }))
      .mockResolvedValueOnce(response([chapter("a", "1")], { previousCursor: null, hasMorePrevious: false, nextCursor: "next-a", hasMoreNext: true }));
    const feed = new ReaderFeedController(fetcher);

    await feed.loadInitial("series", "b");
    await feed.loadNext();
    expect(feed.getState().orderedChapterIds).toEqual(["b", "c"]);
    await feed.loadPrevious();
    expect(feed.getState().orderedChapterIds).toEqual(["a", "b", "c"]);
  });

  it("deduplicates chapters and pages by ID", async () => {
    const fetcher = vi.fn<ReaderChunkFetcher>()
      .mockResolvedValueOnce(response([chapter("a", "1", ["page-1"])] , { nextCursor: "next", hasMoreNext: true }))
      .mockResolvedValueOnce(response([chapter("a", "1", ["page-1", "page-2"]), chapter("b", "2")])) as unknown as ReaderChunkFetcher;
    const feed = new ReaderFeedController(fetcher);

    await feed.loadInitial("series", "a");
    await feed.loadNext();

    expect(feed.getState().orderedChapterIds).toEqual(["a", "b"]);
    expect(feed.getState().chaptersById.a.pages.map((page) => page.id)).toEqual(["page-1", "page-2"]);
    expect(feed.getState().items.filter((item) => item.kind === "page")).toHaveLength(3);
  });

  it("shares simultaneous next and previous requests", async () => {
    let resolveNext!: (value: ReaderChunkResponse) => void;
    let resolvePrevious!: (value: ReaderChunkResponse) => void;
    const fetcher = vi.fn<ReaderChunkFetcher>((request) => {
      if (request.startChapterId) return Promise.resolve(response([chapter("b", "2")], { nextCursor: "next", previousCursor: "previous", hasMoreNext: true, hasMorePrevious: true }));
      if (request.direction === "next") return new Promise((resolve) => { resolveNext = resolve; });
      return new Promise((resolve) => { resolvePrevious = resolve; });
    });
    const feed = new ReaderFeedController(fetcher);
    await feed.loadInitial("series", "b");

    const nextA = feed.loadNext();
    const nextB = feed.loadNext();
    const previousA = feed.loadPrevious();
    const previousB = feed.loadPrevious();
    expect(nextA).toBe(nextB);
    expect(previousA).toBe(previousB);
    expect(fetcher).toHaveBeenCalledTimes(3);
    resolveNext(response([chapter("c", "3")]));
    resolvePrevious(response([chapter("a", "1")]));
    await Promise.all([nextA, previousA]);
  });

  it("ignores stale responses and aborts on disposal", async () => {
    let resolveOld!: (value: ReaderChunkResponse) => void;
    let resolveNew!: (value: ReaderChunkResponse) => void;
    let signal!: AbortSignal;
    const fetcher = vi.fn<ReaderChunkFetcher>((request) => {
      signal = request.signal;
      return new Promise((resolve) => {
        if (request.seriesSlug === "old") resolveOld = resolve;
        else resolveNew = resolve;
      });
    });
    const feed = new ReaderFeedController(fetcher);
    const oldRequest = feed.loadInitial("old", "old-chapter");
    const newRequest = feed.loadInitial("new", "new-chapter");
    resolveOld(response([chapter("old", "1")], { series: { id: "old-series", slug: "old", title: "Old" } }));
    resolveNew(response([chapter("new", "2")], { series: { id: "new-series", slug: "new", title: "New" } }));
    await Promise.allSettled([oldRequest, newRequest]);

    expect(feed.getState().series?.slug).toBe("new");
    feed.dispose();
    expect(signal.aborted).toBe(true);
    expect(feed.getState().orderedChapterIds).toEqual([]);
  });

  it("reports failures and retries only when explicitly requested", async () => {
    const fetcher = vi.fn<ReaderChunkFetcher>()
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce(response([chapter("a", "1")]));
    const feed = new ReaderFeedController(fetcher);

    await expect(feed.loadInitial("series", "a")).rejects.toThrow("network down");
    expect(feed.getState().retryState).toMatchObject({ kind: "initial" });
    await feed.retry();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(feed.getState().error).toBeNull();
  });

  it("stops at cursor exhaustion", async () => {
    const fetcher = vi.fn<ReaderChunkFetcher>().mockResolvedValue(response([chapter("a", "1")]));
    const feed = new ReaderFeedController(fetcher);

    await feed.loadInitial("series", "a");

    expect(feed.loadNext()).toBeNull();
    expect(feed.loadPrevious()).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("moves firstItemIndex by the prepended virtual items", async () => {
    const fetcher = vi.fn<ReaderChunkFetcher>()
      .mockResolvedValueOnce(response([chapter("b", "2")], { previousCursor: "previous", hasMorePrevious: true }))
      .mockResolvedValueOnce(response([chapter("a", "1")]));
    const feed = new ReaderFeedController(fetcher);

    await feed.loadInitial("series", "b");
    const before = feed.getState().firstItemIndex;
    await feed.loadPrevious();

    expect(feed.getState().firstItemIndex).toBe(before - 2);
    expect(feed.getState().orderedChapterIds).toEqual(["a", "b"]);
  });

  it("bounds chapters and pages across one hundred chapters and reloads evicted content", async () => {
    const evictedPages: string[] = [];
    const pagesFor = (id: string) => Array.from({ length: 30 }, (_, index) => `${id}-page-${index}`);
    const fetcher = vi.fn<ReaderChunkFetcher>((request) => {
      if (request.startChapterId) {
        return Promise.resolve(response([chapter("1", "1", pagesFor("1"))], { nextCursor: "next:1", hasMoreNext: true }));
      }
      const [direction, rawId] = (request.cursor ?? "").split(":");
      const id = Number(rawId) + (direction === "next" ? 1 : -1);
      const chapterId = String(id);
      return Promise.resolve(response([chapter(chapterId, chapterId, pagesFor(chapterId))], {
        nextCursor: id < 100 ? `next:${id}` : null,
        previousCursor: id > 1 ? `previous:${id}` : null,
        hasMoreNext: id < 100,
        hasMorePrevious: id > 1,
      }));
    });
    const feed = new ReaderFeedController(fetcher, { onEvict: (pages) => evictedPages.push(...pages.map((page) => page.id)) });

    await feed.loadInitial("series", "1");
    feed.setVisibleChapterId("1");
    const cleanup = vi.fn();
    const unregister = feed.registerPageCleanup("1-page-0", cleanup);
    for (let id = 2; id <= 100; id += 1) {
      await feed.loadNext();
      feed.setVisibleChapterId(String(id));
      const diagnostics = feed.getDiagnostics(feed.getState().items.length, feed.getState().items.filter((item) => item.kind === "page").length);
      expect(diagnostics?.retainedChapterCount).toBeLessThanOrEqual(MAX_RETAINED_CHAPTERS);
      expect(diagnostics?.retainedPageCount).toBeLessThanOrEqual(MAX_RETAINED_PAGES);
    }

    expect(feed.getState().chaptersById["100"]).toBeDefined();
    expect(feed.getState().visibleChapterId).toBe("100");
    expect(new Set(feed.getState().orderedChapterIds).size).toBe(feed.getState().orderedChapterIds.length);
    expect(evictedPages).toContain("1-page-0");
    expect(cleanup).toHaveBeenCalledTimes(1);
    unregister();

    for (let id = 99; id >= 1; id -= 1) {
      feed.setVisibleChapterId(String(id + 1));
      await feed.loadPrevious();
      feed.setVisibleChapterId(String(id));
    }
    expect(feed.getState().chaptersById["1"]).toBeDefined();
    expect(feed.getState().visibleChapterId).toBe("1");
  });

  it("uses exact retained-edge cursors across two-chapter eviction reloads", async () => {
    const requested: Array<{ cursor: string; reason: string }> = [];
    const indexedResponse = (ids: number[]): ReaderChunkResponse => {
      const chapters = ids.map((id) => chapter(String(id), String(id)));
      return response(chapters, {
        previousCursor: ids[0] > 1 ? `previous:${ids[0]}` : null,
        nextCursor: ids.at(-1)! < 12 ? `next:${ids.at(-1)}` : null,
        hasMorePrevious: ids[0] > 1,
        hasMoreNext: ids.at(-1)! < 12,
        boundariesByChapterId: Object.fromEntries(ids.map((id) => [String(id), {
          chapterId: String(id),
          previousCursor: id > 1 ? `previous:${id}` : null,
          nextCursor: id < 12 ? `next:${id}` : null,
          hasMorePrevious: id > 1,
          hasMoreNext: id < 12,
        }])),
      });
    };
    const fetcher = vi.fn<ReaderChunkFetcher>((request) => {
      if (request.startChapterId) return Promise.resolve(indexedResponse([1, 2]));
      requested.push({ cursor: request.cursor ?? "", reason: request.reason });
      const boundary = Number(request.cursor?.split(":")[1]);
      return Promise.resolve(request.direction === "next"
        ? indexedResponse([boundary + 1, boundary + 2].filter((id) => id <= 12))
        : indexedResponse([boundary - 2, boundary - 1].filter((id) => id >= 1)));
    });
    const feed = new ReaderFeedController(fetcher);

    await feed.loadInitial("series", "1");
    for (const visible of [2, 4, 6, 8, 10, 12]) {
      if (visible > 2) await feed.loadNext("endReached");
      feed.setVisibleChapterId(String(visible));
    }
    expect(feed.getState().orderedChapterIds).toEqual(["4", "5", "6", "7", "8", "9", "10", "11", "12"]);
    expect(feed.getState().previousCursor).toBe("previous:4");

    feed.setVisibleChapterId("4");
    await feed.loadPrevious("prepend");
    expect(requested.at(-1)).toEqual({ cursor: "previous:4", reason: "prepend" });
    expect(feed.getState().orderedChapterIds).toEqual(["2", "3", "4", "5", "6", "7", "8", "9", "10"]);
    expect(new Set(feed.getState().orderedChapterIds).size).toBe(feed.getState().orderedChapterIds.length);

    feed.setVisibleChapterId("10");
    await feed.loadNext("endReached");
    expect(requested.at(-1)).toEqual({ cursor: "next:10", reason: "evictionReload" });
    expect(feed.getState().orderedChapterIds).toEqual(["4", "5", "6", "7", "8", "9", "10", "11", "12"]);
  });
});
