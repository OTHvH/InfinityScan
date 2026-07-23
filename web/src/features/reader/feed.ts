import { ApiError } from "@/lib/api";
import { buildReaderItems } from "./items";
import type {
  ReaderChapter,
  ReaderChunkResponse,
  ReaderDirection,
  ReaderFeedState,
  ReaderPage,
  ReaderRetryState,
  ReaderChunkRequest,
} from "./types";

export const MAX_RETAINED_CHAPTERS = 9;
export const MAX_RETAINED_PAGES = 250;
export const RETAIN_BEHIND_VISIBLE = 3;
export const RETAIN_AHEAD_VISIBLE = 5;
const INITIAL_ITEM_INDEX = 100_000;

export interface ReaderRetentionConfig {
  maxRetainedChapters: number;
  maxRetainedPages: number;
  retainBehindVisible: number;
  retainAheadVisible: number;
}

export const DEFAULT_READER_RETENTION: ReaderRetentionConfig = {
  maxRetainedChapters: MAX_RETAINED_CHAPTERS,
  maxRetainedPages: MAX_RETAINED_PAGES,
  retainBehindVisible: RETAIN_BEHIND_VISIBLE,
  retainAheadVisible: RETAIN_AHEAD_VISIBLE,
};

export interface ReaderDiagnostics {
  retainedChapterCount: number;
  retainedPageCount: number;
  renderedItemCount: number;
  renderedPageCount: number;
  activeNextRequests: number;
  activePreviousRequests: number;
}

export type ReaderChunkFetcher = (request: ReaderChunkRequest) => Promise<ReaderChunkResponse>;
type Listener = () => void;
type EvictionCleanup = () => void;

function initialState(): ReaderFeedState {
  return {
    series: null,
    chaptersById: {},
    orderedChapterIds: [],
    items: [],
    nextCursor: null,
    previousCursor: null,
    hasMoreNext: false,
    hasMorePrevious: false,
    visibleChapterId: null,
    initialChapterId: null,
    firstItemIndex: INITIAL_ITEM_INDEX,
    loadingInitial: false,
    loadingNext: false,
    loadingPrevious: false,
    error: null,
    retryState: null,
  };
}

function isAbortError(error: unknown): boolean {
  return (
    (typeof DOMException !== "undefined" && error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

export class ReaderFeedController {
  private state = initialState();
  private generation = 0;
  private controller = new AbortController();
  private readonly listeners = new Set<Listener>();
  private readonly inFlight = new Map<ReaderDirection, Promise<ReaderChunkResponse>>();
  private readonly previousCursors = new Map<string, string | null>();
  private readonly nextCursors = new Map<string, string | null>();
  private readonly protectedChapters = new Set<string>();
  private readonly pageCleanups = new Map<string, Set<EvictionCleanup>>();
  private previousFallbackCursor: string | null = null;
  private nextFallbackCursor: string | null = null;
  private readonly retention: ReaderRetentionConfig;
  private readonly onEvict?: (pages: ReaderPage[]) => void;

  constructor(
    private readonly fetcher: ReaderChunkFetcher,
    options: { retention?: Partial<ReaderRetentionConfig>; onEvict?: (pages: ReaderPage[]) => void } = {},
  ) {
    this.retention = { ...DEFAULT_READER_RETENTION, ...options.retention };
    this.onEvict = options.onEvict;
  }

  getState = (): ReaderFeedState => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  loadInitial(seriesSlug: string, chapterId: string): Promise<ReaderChunkResponse> {
    this.resetForRequest();
    this.state = { ...initialState(), initialChapterId: chapterId, loadingInitial: true };
    this.publish();
    const requestGeneration = this.generation;
    const request = this.fetcher({
      seriesSlug,
      startChapterId: chapterId,
      direction: "next",
      limit: 2,
      signal: this.controller.signal,
    });
    const trackedPromise = request
      .then((response) => {
        if (requestGeneration === this.generation) {
          this.merge(response, "next", true);
          this.state = { ...this.state, loadingInitial: false, error: null, retryState: null };
          this.publish();
        }
        return response;
      })
      .catch((error: unknown) => {
        if (requestGeneration === this.generation && !isAbortError(error)) this.fail(error, { kind: "initial", seriesSlug, chapterId });
        throw error;
      })
      .finally(() => {
        if (this.inFlight.get("next") === trackedPromise) {
          this.inFlight.delete("next");
          if (requestGeneration === this.generation) {
            this.state = { ...this.state };
            this.publish();
          }
        }
      });
    this.inFlight.set("next", trackedPromise);
    return trackedPromise;
  }

  loadNext(): Promise<ReaderChunkResponse> | null {
    return this.load("next");
  }

  loadPrevious(): Promise<ReaderChunkResponse> | null {
    return this.load("previous");
  }

  retry(): Promise<ReaderChunkResponse> | null {
    const retryState = this.state.retryState;
    if (!retryState) return null;
    if (retryState.kind === "initial") return this.loadInitial(retryState.seriesSlug, retryState.chapterId);
    return this.load(retryState.kind);
  }

  setVisibleChapterId(chapterId: string | null): void {
    if (chapterId && !this.state.chaptersById[chapterId]) return;
    if (this.state.visibleChapterId === chapterId) return;
    this.state = { ...this.state, visibleChapterId: chapterId };
    this.trimVisibleWindow();
    this.publish();
  }

  setChapterProtected(chapterId: string, protectedChapter: boolean): void {
    if (protectedChapter) this.protectedChapters.add(chapterId);
    else this.protectedChapters.delete(chapterId);
    this.trimVisibleWindow();
    this.publish();
  }

  registerPageCleanup(pageId: string, cleanup: EvictionCleanup): () => void {
    const cleanups = this.pageCleanups.get(pageId) ?? new Set<EvictionCleanup>();
    cleanups.add(cleanup);
    this.pageCleanups.set(pageId, cleanups);
    return () => {
      cleanups.delete(cleanup);
      if (cleanups.size === 0) this.pageCleanups.delete(pageId);
    };
  }

  getDiagnostics(renderedItemCount = 0, renderedPageCount = 0): ReaderDiagnostics | null {
    return {
      retainedChapterCount: this.state.orderedChapterIds.length,
      retainedPageCount: countUniquePages(this.state.orderedChapterIds, this.state.chaptersById),
      renderedItemCount,
      renderedPageCount,
      activeNextRequests: this.inFlight.has("next") ? 1 : 0,
      activePreviousRequests: this.inFlight.has("previous") ? 1 : 0,
    };
  }

  dispose(): void {
    this.resetForRequest();
    this.protectedChapters.clear();
    this.pageCleanups.clear();
    this.state = initialState();
    this.publish();
  }

  private load(direction: ReaderDirection): Promise<ReaderChunkResponse> | null {
    const cursor = direction === "next" ? this.state.nextCursor : this.state.previousCursor;
    const canLoad = direction === "next" ? this.state.hasMoreNext : this.state.hasMorePrevious;
    const existing = this.inFlight.get(direction);
    if (existing) return existing;
    const series = this.state.series;
    if (!cursor || !canLoad || !series || !this.state.initialChapterId) return null;
    const requestGeneration = this.generation;
    this.state = {
      ...this.state,
      loadingNext: direction === "next" ? true : this.state.loadingNext,
      loadingPrevious: direction === "previous" ? true : this.state.loadingPrevious,
      error: null,
      retryState: null,
    };
    this.publish();
    const request = this.fetcher({
      seriesSlug: series.slug,
      cursor,
      direction,
      limit: 2,
      signal: this.controller.signal,
    });
    const trackedPromise = request
      .then((response) => {
        if (requestGeneration === this.generation) {
          this.merge(response, direction, false);
          this.state = {
            ...this.state,
            loadingNext: direction === "next" ? false : this.state.loadingNext,
            loadingPrevious: direction === "previous" ? false : this.state.loadingPrevious,
            error: null,
            retryState: null,
          };
          this.publish();
        }
        return response;
      })
      .catch((error: unknown) => {
        if (requestGeneration === this.generation && !isAbortError(error)) {
          this.fail(error, { kind: direction });
        }
        throw error;
      })
      .finally(() => {
        if (this.inFlight.get(direction) === trackedPromise) {
          this.inFlight.delete(direction);
          if (requestGeneration === this.generation) {
            this.state = { ...this.state };
            this.publish();
          }
        }
      });
    this.inFlight.set(direction, trackedPromise);
    return trackedPromise;
  }

  private merge(response: ReaderChunkResponse, direction: ReaderDirection, initial: boolean): void {
    const incomingById = new Map(response.chapters.map((chapter) => [chapter.id, chapter]));
    const currentById = { ...this.state.chaptersById };
    for (const [id, chapter] of incomingById) currentById[id] = mergeChapter(currentById[id], chapter);
    const incomingIds = [...incomingById.keys()];
    const currentIds = this.state.orderedChapterIds.filter((id) => !incomingById.has(id));
    let orderedIds = initial
      ? incomingIds
      : direction === "previous"
        ? [...incomingIds, ...currentIds]
        : [...currentIds, ...incomingIds];
    const wasEmpty = this.state.orderedChapterIds.length === 0;
    if (incomingIds.length > 0) {
      this.previousCursors.set(incomingIds[0], response.previousCursor);
      this.nextCursors.set(incomingIds[incomingIds.length - 1], response.nextCursor);
      this.previousFallbackCursor = response.previousCursor;
      this.nextFallbackCursor = response.nextCursor;
    }
    let firstItemIndex = this.state.firstItemIndex;
    if (direction === "previous" && !initial) firstItemIndex -= countItemsForIds(incomingIds, currentById);
    const trimmed = this.trimWindow(orderedIds, currentById, firstItemIndex, new Set(incomingIds));
    orderedIds = trimmed.orderedIds;
    firstItemIndex = trimmed.firstItemIndex;
    const firstId = orderedIds[0];
    const lastId = orderedIds[orderedIds.length - 1];
    const previousCursor = firstId && this.previousCursors.has(firstId)
      ? this.previousCursors.get(firstId) ?? null
      : this.previousFallbackCursor;
    const nextCursor = lastId && this.nextCursors.has(lastId)
      ? this.nextCursors.get(lastId) ?? null
      : this.nextFallbackCursor;
    this.state = {
      ...this.state,
      series: response.series,
      chaptersById: Object.fromEntries(orderedIds.map((id) => [id, currentById[id]])),
      orderedChapterIds: orderedIds,
      items: buildReaderItems(currentById, orderedIds),
      nextCursor,
      previousCursor,
      hasMoreNext: nextCursor !== null,
      hasMorePrevious: previousCursor !== null,
      visibleChapterId: this.state.visibleChapterId ?? incomingIds[0] ?? null,
      initialChapterId: this.state.initialChapterId,
      firstItemIndex: wasEmpty && initial ? INITIAL_ITEM_INDEX : firstItemIndex,
    };
  }

  private trimVisibleWindow(): void {
    const trimmed = this.trimWindow(
      [...this.state.orderedChapterIds],
      { ...this.state.chaptersById },
      this.state.firstItemIndex,
    );
    if (
      trimmed.orderedIds.length === this.state.orderedChapterIds.length &&
      trimmed.firstItemIndex === this.state.firstItemIndex
    ) return;
    const chaptersById = Object.fromEntries(
      trimmed.orderedIds.map((id) => [id, this.state.chaptersById[id]]),
    );
    this.state = {
      ...this.state,
      chaptersById,
      orderedChapterIds: trimmed.orderedIds,
      items: buildReaderItems(chaptersById, trimmed.orderedIds),
      firstItemIndex: trimmed.firstItemIndex,
      previousCursor: this.boundaryCursor(trimmed.orderedIds[0], this.previousCursors, this.previousFallbackCursor),
      nextCursor: this.boundaryCursor(trimmed.orderedIds.at(-1), this.nextCursors, this.nextFallbackCursor),
    };
  }

  private trimWindow(
    initialIds: string[],
    chaptersById: Record<string, ReaderChapter>,
    initialItemIndex: number,
    protectedIds = new Set<string>(),
  ): { orderedIds: string[]; firstItemIndex: number } {
    const orderedIds = [...initialIds];
    let firstItemIndex = initialItemIndex;
    const visibleIndex = () => {
      const index = orderedIds.indexOf(this.state.visibleChapterId ?? "");
      return index >= 0 ? index : 0;
    };
    const needsTrim = () =>
      orderedIds.length > this.retention.maxRetainedChapters ||
      countUniquePages(orderedIds, chaptersById) > this.retention.maxRetainedPages;
    while (needsTrim()) {
      const currentVisibleIndex = visibleIndex();
      const keepStart = Math.max(0, currentVisibleIndex - this.retention.retainBehindVisible);
      const keepEnd = Math.min(
        orderedIds.length,
        currentVisibleIndex + this.retention.retainAheadVisible + 1,
      );
      const candidates = orderedIds
        .map((id, index) => ({ id, index }))
        .filter(({ id, index }) =>
          id !== this.state.visibleChapterId &&
          !protectedIds.has(id) &&
          !this.protectedChapters.has(id) &&
          (orderedIds.length > this.retention.maxRetainedChapters || index < keepStart || index >= keepEnd),
        );
      if (candidates.length === 0) break;
      const candidate = candidates.reduce((furthest, current) =>
        Math.abs(current.index - currentVisibleIndex) > Math.abs(furthest.index - currentVisibleIndex)
          ? current
          : furthest,
      );
      const chapter = chaptersById[candidate.id];
      if (candidate.index === 0) firstItemIndex += countItems(chapter);
      this.evictChapter(candidate.id, chapter);
      delete chaptersById[candidate.id];
      orderedIds.splice(candidate.index, 1);
    }
    return { orderedIds, firstItemIndex };
  }

  private evictChapter(chapterId: string, chapter: ReaderChapter | undefined): void {
    const pages = chapter?.pages ?? [];
    this.onEvict?.(pages);
    for (const page of pages) {
      const cleanups = this.pageCleanups.get(page.id);
      cleanups?.forEach((cleanup) => cleanup());
      this.pageCleanups.delete(page.id);
    }
    this.removeCursor(chapterId);
  }

  private boundaryCursor(
    chapterId: string | undefined,
    cursors: Map<string, string | null>,
    fallback: string | null,
  ): string | null {
    return chapterId && cursors.has(chapterId) ? cursors.get(chapterId) ?? null : fallback;
  }

  private fail(error: unknown, retryState: Exclude<ReaderRetryState, null>): void {
    const message = error instanceof ApiError ? error.detail : error instanceof Error ? error.message : "Reader request failed";
    this.state = {
      ...this.state,
      loadingInitial: false,
      loadingNext: false,
      loadingPrevious: false,
      error: message,
      retryState,
    };
    this.publish();
  }

  private resetForRequest(): void {
    this.generation += 1;
    this.controller.abort();
    this.controller = new AbortController();
    this.inFlight.clear();
    this.previousCursors.clear();
    this.nextCursors.clear();
    this.previousFallbackCursor = null;
    this.nextFallbackCursor = null;
    this.pageCleanups.forEach((cleanups) => cleanups.forEach((cleanup) => cleanup()));
    this.pageCleanups.clear();
    this.protectedChapters.clear();
  }

  private removeCursor(chapterId: string): void {
    this.previousCursors.delete(chapterId);
    this.nextCursors.delete(chapterId);
  }

  private publish(): void {
    for (const listener of this.listeners) listener();
  }
}

function mergeChapter(current: ReaderChapter | undefined, incoming: ReaderChapter): ReaderChapter {
  if (!current) return incoming;
  const pagesById = new Map(current.pages.map((page) => [page.id, page]));
  for (const page of incoming.pages) pagesById.set(page.id, page);
  return {
    ...current,
    ...incoming,
    pages: [...pagesById.values()].sort((left, right) => left.pageNumber - right.pageNumber),
  };
}

function countItemsForIds(chapterIds: string[], chaptersById: Record<string, ReaderChapter>): number {
  return chapterIds.reduce((count, id) => count + countItems(chaptersById[id]), 0);
}

function countItems(chapter: ReaderChapter | undefined): number {
  return chapter ? chapter.pages.length + 1 : 0;
}

function countUniquePages(
  chapterIds: string[],
  chaptersById: Record<string, ReaderChapter>,
): number {
  const pageIds = new Set<string>();
  for (const chapterId of chapterIds) {
    for (const page of chaptersById[chapterId]?.pages ?? []) pageIds.add(page.id);
  }
  return pageIds.size;
}
