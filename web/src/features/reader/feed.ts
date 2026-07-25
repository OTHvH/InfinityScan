import { ApiError } from "@/lib/api";
import { buildReaderItems } from "./items";
import type {
  ReaderChapter,
  ReaderChunkResponse,
  ReaderChapterBoundary,
  ReaderDirection,
  ReaderFeedState,
  ReaderPage,
  ReaderRetryState,
  ReaderChunkRequest,
  ReaderRequestReason,
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
let controllerSequence = 0;

export interface ReaderRequestDebugEvent {
  phase: "start" | "settle";
  controllerId: number;
  generation: number;
  requestId: number;
  direction: ReaderDirection;
  cursor: string;
  reason: ReaderRequestReason;
  outcome?: "succeeded" | "failed" | "cancelled";
  retainedChapterIds: string[];
  evictedChapterIds: string[];
}

function emitRequestDebug(event: ReaderRequestDebugEvent): void {
  if (typeof window === "undefined" || localStorage.getItem("infinityscan_reader_debug") !== "1") return;
  window.dispatchEvent(new CustomEvent("infinityscan:reader-request", { detail: event }));
}

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
  private readonly controllerId = ++controllerSequence;
  private requestSequence = 0;
  private state = initialState();
  private generation = 0;
  private controller = new AbortController();
  private readonly listeners = new Set<Listener>();
  private readonly inFlight = new Map<ReaderDirection, Promise<ReaderChunkResponse>>();
  private readonly boundaries = new Map<string, ReaderChapterBoundary>();
  private readonly cursorHistory = new Map<string, Set<string>>();
  private readonly evictedChapterIds = new Set<string>();
  private readonly protectedChapters = new Set<string>();
  private readonly pageCleanups = new Map<string, Set<EvictionCleanup>>();
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

  loadInitial(
    seriesSlug: string,
    chapterId: string,
    reason: Extract<ReaderRequestReason, "initial" | "resume"> = "initial",
  ): Promise<ReaderChunkResponse> {
    this.resetForRequest();
    this.state = { ...initialState(), initialChapterId: chapterId, loadingInitial: true };
    this.publish();
    const requestGeneration = this.generation;
    const requestId = ++this.requestSequence;
    let outcome: "succeeded" | "failed" | "cancelled" = "succeeded";
    const request = this.fetcher({
      seriesSlug,
      startChapterId: chapterId,
      direction: "next",
      limit: 2,
      signal: this.controller.signal,
      reason,
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
        outcome = isAbortError(error) ? "cancelled" : "failed";
        if (requestGeneration === this.generation && !isAbortError(error)) this.fail(error, { kind: "initial", seriesSlug, chapterId });
        throw error;
      })
      .finally(() => {
        emitRequestDebug({
          phase: "settle",
          controllerId: this.controllerId,
          generation: requestGeneration,
          requestId,
          direction: "next",
          cursor: `start:${chapterId}`,
          reason,
          outcome,
          retainedChapterIds: [...this.state.orderedChapterIds],
          evictedChapterIds: [...this.evictedChapterIds],
        });
        if (this.inFlight.get("next") === trackedPromise) {
          this.inFlight.delete("next");
          if (requestGeneration === this.generation) {
            this.state = { ...this.state };
            this.publish();
          }
        }
      });
    this.inFlight.set("next", trackedPromise);
    emitRequestDebug({
      phase: "start",
      controllerId: this.controllerId,
      generation: requestGeneration,
      requestId,
      direction: "next",
      cursor: `start:${chapterId}`,
      reason,
      retainedChapterIds: [],
      evictedChapterIds: [...this.evictedChapterIds],
    });
    return trackedPromise;
  }

  loadNext(reason: Extract<ReaderRequestReason, "endReached" | "footerObserver" | "modeTransition"> = "endReached"): Promise<ReaderChunkResponse> | null {
    return this.load("next", reason);
  }

  loadPrevious(reason: Extract<ReaderRequestReason, "prepend" | "modeTransition"> = "prepend"): Promise<ReaderChunkResponse> | null {
    return this.load("previous", reason);
  }

  retry(): Promise<ReaderChunkResponse> | null {
    const retryState = this.state.retryState;
    if (!retryState) return null;
    if (retryState.kind === "initial") return this.loadInitial(retryState.seriesSlug, retryState.chapterId, "initial");
    return this.load(retryState.kind, "retry");
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

  private load(direction: ReaderDirection, requestedReason: ReaderRequestReason): Promise<ReaderChunkResponse> | null {
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
    const priorChapterIds = this.cursorHistory.get(cursor);
    const reason = priorChapterIds?.size && [...priorChapterIds].some((id) => !this.state.chaptersById[id])
      ? "evictionReload"
      : requestedReason;
    const requestId = ++this.requestSequence;
    let outcome: "succeeded" | "failed" | "cancelled" = "succeeded";
    const request = this.fetcher({
      seriesSlug: series.slug,
      cursor,
      direction,
      limit: 2,
      signal: this.controller.signal,
      reason,
    });
    const trackedPromise = request
      .then((response) => {
        if (requestGeneration === this.generation) {
          this.cursorHistory.set(cursor, new Set(response.chapters.map((chapter) => chapter.id)));
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
        outcome = isAbortError(error) ? "cancelled" : "failed";
        if (requestGeneration === this.generation && !isAbortError(error)) {
          this.fail(error, { kind: direction });
        }
        throw error;
      })
      .finally(() => {
        emitRequestDebug({
          phase: "settle",
          controllerId: this.controllerId,
          generation: requestGeneration,
          requestId,
          direction,
          cursor,
          reason,
          outcome,
          retainedChapterIds: [...this.state.orderedChapterIds],
          evictedChapterIds: [...this.evictedChapterIds],
        });
        if (this.inFlight.get(direction) === trackedPromise) {
          this.inFlight.delete(direction);
          if (requestGeneration === this.generation) {
            this.state = { ...this.state };
            this.publish();
          }
        }
      });
    this.inFlight.set(direction, trackedPromise);
    emitRequestDebug({
      phase: "start",
      controllerId: this.controllerId,
      generation: requestGeneration,
      requestId,
      direction,
      cursor,
      reason,
      retainedChapterIds: [...this.state.orderedChapterIds],
      evictedChapterIds: [...this.evictedChapterIds],
    });
    this.publish();
    return trackedPromise;
  }

  private merge(response: ReaderChunkResponse, direction: ReaderDirection, initial: boolean): void {
    const incomingById = new Map(response.chapters.map((chapter) => [chapter.id, chapter]));
    const currentById = { ...this.state.chaptersById };
    for (const [id, chapter] of incomingById) currentById[id] = mergeChapter(currentById[id], chapter);
    const incomingIds = [...incomingById.keys()];
    const currentIds = [...this.state.orderedChapterIds];
    const currentIdSet = new Set(currentIds);
    const novelIds = incomingIds.filter((id) => !currentIdSet.has(id));
    let orderedIds = initial
      ? incomingIds
      : direction === "previous"
        ? [...novelIds, ...currentIds]
        : [...currentIds, ...novelIds];
    const wasEmpty = this.state.orderedChapterIds.length === 0;
    for (const [chapterId, boundary] of Object.entries(response.boundariesByChapterId)) {
      this.boundaries.set(chapterId, boundary);
      this.evictedChapterIds.delete(chapterId);
    }
    let firstItemIndex = this.state.firstItemIndex;
    if (direction === "previous" && !initial) firstItemIndex -= countItemsForIds(novelIds, currentById);
    const trimmed = this.trimWindow(orderedIds, currentById, firstItemIndex, new Set(incomingIds));
    orderedIds = trimmed.orderedIds;
    firstItemIndex = trimmed.firstItemIndex;
    this.state = this.withEdgeMetadata({
      ...this.state,
      series: response.series,
      chaptersById: Object.fromEntries(orderedIds.map((id) => [id, currentById[id]])),
      orderedChapterIds: orderedIds,
      items: buildReaderItems(currentById, orderedIds),
      visibleChapterId: this.state.visibleChapterId ?? incomingIds[0] ?? null,
      initialChapterId: this.state.initialChapterId,
      firstItemIndex: wasEmpty && initial ? INITIAL_ITEM_INDEX : firstItemIndex,
    }, orderedIds);
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
    this.state = this.withEdgeMetadata({
      ...this.state,
      chaptersById,
      orderedChapterIds: trimmed.orderedIds,
      items: buildReaderItems(chaptersById, trimmed.orderedIds),
      firstItemIndex: trimmed.firstItemIndex,
    }, trimmed.orderedIds);
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
          (index === 0 || index === orderedIds.length - 1) &&
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
    this.boundaries.delete(chapterId);
    this.evictedChapterIds.add(chapterId);
  }

  private withEdgeMetadata(state: ReaderFeedState, orderedIds: string[]): ReaderFeedState {
    const first = this.boundaries.get(orderedIds[0] ?? "");
    const last = this.boundaries.get(orderedIds.at(-1) ?? "");
    return {
      ...state,
      previousCursor: first?.previousCursor ?? null,
      nextCursor: last?.nextCursor ?? null,
      hasMorePrevious: first?.hasMorePrevious ?? false,
      hasMoreNext: last?.hasMoreNext ?? false,
    };
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
    this.boundaries.clear();
    this.cursorHistory.clear();
    this.evictedChapterIds.clear();
    this.pageCleanups.forEach((cleanups) => cleanups.forEach((cleanup) => cleanup()));
    this.pageCleanups.clear();
    this.protectedChapters.clear();
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
