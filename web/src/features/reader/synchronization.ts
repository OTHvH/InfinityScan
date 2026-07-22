import type { ListRange } from "react-virtuoso";
import type { ReaderChapter, ReaderItem } from "./types";

export interface VisibleReaderPage {
  chapterId: string;
  pageId: string;
  pageNumber: number;
  scrollRatio: number;
}

interface RouterReplace {
  replace: (href: string, options: { scroll: false }) => void;
}

function localRangeIndex(index: number, firstItemIndex: number, itemCount: number): number {
  if (index >= firstItemIndex && index < firstItemIndex + itemCount) return index - firstItemIndex;
  return index;
}

export function selectViewportCenterPage(
  items: ReaderItem[],
  range: ListRange,
  firstItemIndex: number,
  chaptersById: Record<string, ReaderChapter>,
): VisibleReaderPage | null {
  if (items.length === 0) return null;
  const start = Math.max(0, localRangeIndex(range.startIndex, firstItemIndex, items.length));
  const end = Math.min(items.length - 1, localRangeIndex(range.endIndex, firstItemIndex, items.length));
  const center = (start + end) / 2;
  let selectedIndex = -1;
  let selectedDistance = Number.POSITIVE_INFINITY;

  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (item.kind !== "page") continue;
    const outsideDistance = index < start ? start - index : index > end ? index - end : 0;
    const distance = outsideDistance * (items.length + 1) + Math.abs(index - center);
    if (distance < selectedDistance) {
      selectedIndex = index;
      selectedDistance = distance;
    }
  }

  const selected = items[selectedIndex];
  if (!selected || selected.kind !== "page") return null;
  const pages = chaptersById[selected.chapterId]?.pages ?? [];
  const pageIndex = pages.findIndex((page) => page.id === selected.pageId);
  const scrollRatio = pages.length <= 1
    ? 0
    : Math.min(1, Math.max(0, (pageIndex < 0 ? selected.pageNumber - 1 : pageIndex) / (pages.length - 1)));
  return {
    chapterId: selected.chapterId,
    pageId: selected.pageId,
    pageNumber: selected.pageNumber,
    scrollRatio,
  };
}

export function canonicalReaderUrl(seriesSlug: string, chapterId: string): string {
  return `/reader/${encodeURIComponent(seriesSlug)}/${encodeURIComponent(chapterId)}`;
}

export class ReaderUrlSynchronizer {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private routedChapterId: string;

  constructor(
    private readonly router: RouterReplace,
    private readonly seriesSlug: string,
    initialChapterId: string,
    private readonly debounceMs = 250,
  ) {
    this.routedChapterId = initialChapterId;
  }

  update(chapterId: string): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (chapterId === this.routedChapterId) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.routedChapterId = chapterId;
      this.router.replace(canonicalReaderUrl(this.seriesSlug, chapterId), { scroll: false });
    }, this.debounceMs);
  }

  dispose(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
