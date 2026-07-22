export type ReaderDirection = "next" | "previous";
export type ReadingMode = "continuous" | "vertical" | "horizontal";
export type SpreadMode = "single" | "spread";
export type ReadingDirection = "ltr" | "rtl";

export interface ReaderSeries {
  id: string;
  slug: string;
  title: string;
}

export interface ReaderPage {
  id: string;
  pageNumber: number;
  mediaPath: string;
  width: number;
  height: number;
  aspectRatio: number;
}

export interface ReaderChapter {
  id: string;
  number: string;
  title: string | null;
  pageCount: number;
  previousChapterId: string | null;
  nextChapterId: string | null;
  pages: ReaderPage[];
}

export interface ReaderChunkResponse {
  series: ReaderSeries;
  chapters: ReaderChapter[];
  nextCursor: string | null;
  previousCursor: string | null;
  hasMoreNext: boolean;
  hasMorePrevious: boolean;
}

export interface ReaderCursorState {
  nextCursor: string | null;
  previousCursor: string | null;
  hasMoreNext: boolean;
  hasMorePrevious: boolean;
}

export type ReaderRetryState =
  | { kind: "initial"; seriesSlug: string; chapterId: string }
  | { kind: "next" }
  | { kind: "previous" }
  | null;

export interface ReaderFeedState extends ReaderCursorState {
  series: ReaderSeries | null;
  chaptersById: Record<string, ReaderChapter>;
  orderedChapterIds: string[];
  items: ReaderItem[];
  visibleChapterId: string | null;
  initialChapterId: string | null;
  firstItemIndex: number;
  loadingInitial: boolean;
  loadingNext: boolean;
  loadingPrevious: boolean;
  error: string | null;
  retryState: ReaderRetryState;
}

export type ReaderItem =
  | {
      kind: "chapter-separator";
      key: string;
      chapterId: string;
      chapterNumber: string;
      title: string | null;
    }
  | ({
      kind: "page";
      key: string;
      chapterId: string;
      chapterNumber: string;
      pageId: string;
      pageNumber: number;
      mediaPath: string;
      width: number;
      height: number;
      aspectRatio: number;
    });

export interface ReaderChunkRequest {
  seriesSlug: string;
  startChapterId?: string;
  cursor?: string;
  direction: ReaderDirection;
  limit: number;
  signal: AbortSignal;
}
