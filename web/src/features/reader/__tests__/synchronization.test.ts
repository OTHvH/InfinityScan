import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReaderChapter, ReaderItem } from "../types";
import {
  ReaderUrlSynchronizer,
  canonicalReaderUrl,
  selectViewportCenterPage,
} from "../synchronization";

function chapter(id: string, pageCount: number): ReaderChapter {
  return {
    id,
    number: id === "chapter-a" ? "99.5" : "1",
    title: null,
    pageCount,
    previousChapterId: null,
    nextChapterId: null,
    pages: Array.from({ length: pageCount }, (_, index) => ({
      id: `${id}-page-${index + 1}`,
      pageNumber: index + 1,
      mediaPath: `/media/pages/${id}-${index + 1}`,
      width: 100,
      height: 150,
      aspectRatio: 2 / 3,
    })),
  };
}

function itemsFor(chapters: ReaderChapter[]): ReaderItem[] {
  return chapters.flatMap((item) => [
    {
      kind: "chapter-separator" as const,
      key: `chapter:${item.id}`,
      chapterId: item.id,
      chapterNumber: item.number,
      title: item.title,
    },
    ...item.pages.map((page) => ({
      kind: "page" as const,
      key: `page:${page.id}`,
      chapterId: item.id,
      chapterNumber: item.number,
      pageId: page.id,
      pageNumber: page.pageNumber,
      mediaPath: page.mediaPath,
      width: page.width,
      height: page.height,
      aspectRatio: page.aspectRatio,
    })),
  ]);
}

afterEach(() => vi.useRealTimers());

describe("visible reader item selection", () => {
  it("selects the visible page nearest the viewport center using item metadata", () => {
    const chapters = [chapter("chapter-a", 3), chapter("chapter-b", 3)];
    const items = itemsFor(chapters);

    const visible = selectViewportCenterPage(
      items,
      { startIndex: 100_002, endIndex: 100_005 },
      100_000,
      Object.fromEntries(chapters.map((item) => [item.id, item])),
    );

    expect(visible).toEqual({
      chapterId: "chapter-a",
      pageId: "chapter-a-page-3",
      pageNumber: 3,
      scrollRatio: 1,
    });
  });

  it("uses chapter IDs rather than chapter-number arithmetic at a transition", () => {
    const chapters = [chapter("chapter-a", 2), chapter("chapter-b", 2)];
    const items = itemsFor(chapters);
    const visible = selectViewportCenterPage(
      items,
      { startIndex: 3, endIndex: 5 },
      100_000,
      Object.fromEntries(chapters.map((item) => [item.id, item])),
    );

    expect(visible?.chapterId).toBe("chapter-b");
    expect(visible?.pageId).toBe("chapter-b-page-1");
  });
});

describe("reader URL synchronization", () => {
  it("debounces chapter changes and replaces without scrolling or growing history", () => {
    vi.useFakeTimers();
    const router = { replace: vi.fn(), push: vi.fn() };
    const synchronizer = new ReaderUrlSynchronizer(router, "series slug", "chapter-a", 100);

    synchronizer.update("chapter-b");
    synchronizer.update("chapter-c");
    vi.advanceTimersByTime(99);
    expect(router.replace).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);

    expect(router.replace).toHaveBeenCalledOnce();
    expect(router.replace).toHaveBeenCalledWith(
      "/reader/series%20slug/chapter-c",
      { scroll: false },
    );
    expect(router.push).not.toHaveBeenCalled();
    synchronizer.dispose();
  });

  it("does not replace the already canonical requested chapter", () => {
    vi.useFakeTimers();
    const router = { replace: vi.fn() };
    const synchronizer = new ReaderUrlSynchronizer(router, "series", "chapter-a", 10);
    synchronizer.update("chapter-a");
    vi.runAllTimers();
    expect(router.replace).not.toHaveBeenCalled();
    expect(canonicalReaderUrl("series", "chapter-a")).toBe("/reader/series/chapter-a");
  });
});
