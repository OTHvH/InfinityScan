import { ApiError } from "@/lib/api";
import { buildReaderItems } from "./items";
import type {
  ReaderChapter,
  ReaderChunkResponse,
  ReaderDirection,
  ReaderFeedState,
  ReaderRetryState,
  ReaderChunkRequest,
} from "./types";

const MAX_RETAINED_CHAPTERS = 4;
const INITIAL_ITEM_INDEX = 100_000;

export type ReaderChunkFetcher = (request: ReaderChunkRequest) => Promise<ReaderChunkResponse>;
type Listener = () => void;

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
  private fetcher: ReaderChunkFetcher;

  constructor(fetcher: ReaderChunkFetcher) {
    this.fetcher = fetcher;
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
        if (this.inFlight.get("next") === trackedPromise) this.inFlight.delete("next");
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
    if (this.state.visibleChapterId === chapterId) return;
    this.state = { ...this.state, visibleChapterId: chapterId };
    this.publish();
  }

  dispose(): void {
    this.resetForRequest();
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
        if (this.inFlight.get(direction) === trackedPromise) this.inFlight.delete(direction);
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
    const orderedIds = initial
      ? incomingIds
      : direction === "previous"
        ? [...incomingIds, ...currentIds]
        : [...currentIds, ...incomingIds];
    const wasEmpty = this.state.orderedChapterIds.length === 0;
    if (incomingIds.length > 0) {
      this.previousCursors.set(incomingIds[0], response.previousCursor);
      this.nextCursors.set(incomingIds[incomingIds.length - 1], response.nextCursor);
    }
    if (direction === "previous" && !initial) {
      this.state = { ...this.state, firstItemIndex: this.state.firstItemIndex - countPages(incomingIds, currentById) };
    }
    while (orderedIds.length > MAX_RETAINED_CHAPTERS) {
      if (direction === "previous") {
        const removed = orderedIds.pop();
        if (removed) this.removeCursor(removed);
      } else {
        const removed = orderedIds.shift();
        if (removed) {
          this.state = { ...this.state, firstItemIndex: this.state.firstItemIndex + (currentById[removed]?.pages.length ?? 0) };
          this.removeCursor(removed);
        }
      }
    }
    const firstId = orderedIds[0];
    const lastId = orderedIds[orderedIds.length - 1];
    const previousCursor = firstId && this.previousCursors.has(firstId)
      ? this.previousCursors.get(firstId) ?? null
      : response.previousCursor;
    const nextCursor = lastId && this.nextCursors.has(lastId)
      ? this.nextCursors.get(lastId) ?? null
      : response.nextCursor;
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
      firstItemIndex: wasEmpty && initial ? INITIAL_ITEM_INDEX : this.state.firstItemIndex,
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
    this.previousCursors.clear();
    this.nextCursors.clear();
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

function countPages(chapterIds: string[], chaptersById: Record<string, ReaderChapter>): number {
  return chapterIds.reduce((count, id) => count + (chaptersById[id]?.pages.length ?? 0), 0);
}
