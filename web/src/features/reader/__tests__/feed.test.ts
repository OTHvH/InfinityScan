import { describe, expect, it, vi } from "vitest";
import { ReaderFeedController, type ReaderChunkFetcher } from "../feed";
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
  return {
    series: { id: "series-id", slug: "series", title: "Series" },
    chapters,
    nextCursor: null,
    previousCursor: null,
    hasMoreNext: false,
    hasMorePrevious: false,
    ...options,
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
});
