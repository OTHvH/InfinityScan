"use client";

import { startTransition, use, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { fetchReaderChunk } from "@/features/reader/api";
import { ContinuousReader } from "@/features/reader/components/ContinuousReader";
import { movePagedIndex, PagedReader } from "@/features/reader/components/PagedReader";
import { clampReaderZoom, ReaderToolbar, READER_ZOOM_STEP } from "@/features/reader/components/ReaderToolbar";
import { ReaderFeedController } from "@/features/reader/feed";
import { getReaderKeyboardAction, isReaderInputTarget } from "@/features/reader/keyboard";
import { getAdjacentChapterId, getLoadedAdjacentChapterId } from "@/features/reader/navigation";
import { DEFAULT_READER_PREFERENCES, loadReaderPreferences, saveReaderPreferences } from "@/features/reader/preferences";
import {
  ReaderProgressController,
  attachProgressFlushListeners,
  chooseNewestProgress,
  clampProgress,
  loadLocalProgress,
  loadServerProgress,
  type ProgressStatus,
} from "@/features/reader/progress";
import { ReaderUrlSynchronizer, selectViewportCenterPage } from "@/features/reader/synchronization";
import type { ReaderChapter, ReaderDirection, ReaderPage, ReaderRequestReason, ReadingDirection, ReadingMode, SpreadMode } from "@/features/reader/types";
import { useAuthStore } from "@/stores/auth";

interface ReaderProps {
  params: Promise<{ slug: string; chapter: string }>;
}

function KeyboardHint({ visible }: { visible: boolean }) {
  if (!visible) return null;
  return (
    <div className="keyboard-hint" role="note">
      <span>Arrow keys: pages in paged modes</span>
      <span>Space: next page or spread</span>
      <span>[ / ]: previous / next chapter</span>
      <span>F: fit width</span>
      <span>?: close help</span>
    </div>
  );
}

export default function EnhancedReader({ params }: ReaderProps) {
  const { slug: seriesSlug, chapter: routeChapterId } = use(params);
  const router = useRouter();
  const user = useAuthStore((state) => state.user);
  const authInitializing = useAuthStore((state) => state.isInitializing);
  const [requestedChapterId] = useState(routeChapterId);
  const [feed] = useState(() => new ReaderFeedController(fetchReaderChunk));
  const feedState = useSyncExternalStore(feed.subscribe, feed.getState, feed.getState);
  const [currentChapterId, setCurrentChapterId] = useState(requestedChapterId);
  const [currentPage, setCurrentPage] = useState(1);
  const [currentPageId, setCurrentPageId] = useState<string | null>(null);
  const [currentScrollRatio, setCurrentScrollRatio] = useState(0);
  const [readingMode, setReadingMode] = useState<ReadingMode>(DEFAULT_READER_PREFERENCES.mode);
  const [spreadMode, setSpreadMode] = useState<SpreadMode>(DEFAULT_READER_PREFERENCES.spreadMode);
  const [zoom, setZoom] = useState(DEFAULT_READER_PREFERENCES.zoom);
  const [fitWidth, setFitWidth] = useState(DEFAULT_READER_PREFERENCES.fitWidth);
  const [readingDirection, setReadingDirection] = useState<ReadingDirection>(DEFAULT_READER_PREFERENCES.direction);
  const [firstPageAlone, setFirstPageAlone] = useState(DEFAULT_READER_PREFERENCES.firstPageAlone);
  const [showHint, setShowHint] = useState(false);
  const [resumeReady, setResumeReady] = useState(false);
  const [progressStatus, setProgressStatus] = useState<ProgressStatus>("saved");
  const resumeStarted = useRef(false);
  const nextRequestReason = useRef<Extract<ReaderRequestReason, "modeTransition"> | null>(null);
  const [urlSynchronizer] = useState(() => new ReaderUrlSynchronizer(router, seriesSlug, requestedChapterId));
  const [progressController] = useState(() => new ReaderProgressController({
    seriesSlug,
    authenticated: !!useAuthStore.getState().user,
    onStatus: setProgressStatus,
  }));

  useEffect(() => {
    const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
    const reason = navigation?.type === "reload" || navigation?.type === "back_forward" ? "resume" : "initial";
    void feed.loadInitial(seriesSlug, requestedChapterId, reason).catch(() => undefined);
    return () => feed.dispose();
  }, [feed, requestedChapterId, seriesSlug]);

  useEffect(() => {
    progressController.setAuthenticated(!!user);
  }, [progressController, user]);

  const loadNext = useCallback((reason: Extract<ReaderRequestReason, "endReached" | "footerObserver" | "modeTransition">) => {
    const effectiveReason = nextRequestReason.current ?? reason;
    nextRequestReason.current = null;
    return feed.loadNext(effectiveReason);
  }, [feed]);
  const loadPrevious = useCallback((reason: Extract<ReaderRequestReason, "prepend" | "modeTransition">) => feed.loadPrevious(reason), [feed]);
  const retryFeed = useCallback(() => feed.retry(), [feed]);
  const registerPageCleanup = useCallback(
    (pageId: string, cleanup: () => void) => feed.registerPageCleanup(pageId, cleanup),
    [feed],
  );

  const activeChapter = feedState.chaptersById[currentChapterId]
    ?? (feedState.orderedChapterIds[0] ? feedState.chaptersById[feedState.orderedChapterIds[0]] : undefined);
  const activePages = useMemo(() => activeChapter?.pages ?? [], [activeChapter]);

  const setActivePage = useCallback((chapter: ReaderChapter, page: ReaderPage | undefined) => {
    const selected = page ?? chapter.pages[0];
    if (!selected) return;
    const pageIndex = Math.max(0, chapter.pages.findIndex((item) => item.id === selected.id));
    startTransition(() => {
      setCurrentChapterId(chapter.id);
      setCurrentPage(selected.pageNumber);
      setCurrentPageId(selected.id);
      setCurrentScrollRatio(chapter.pages.length <= 1 ? 0 : pageIndex / (chapter.pages.length - 1));
    });
    feed.setVisibleChapterId(chapter.id);
  }, [feed]);

  const goToPage = useCallback((pageNumber: number) => {
    if (!activeChapter) return;
    const exact = activePages.find((page) => page.pageNumber === pageNumber);
    const boundedIndex = Math.min(activePages.length - 1, Math.max(0, pageNumber - 1));
    setActivePage(activeChapter, exact ?? activePages[boundedIndex]);
  }, [activeChapter, activePages, setActivePage]);

  const movePage = useCallback((delta: -1 | 1) => {
    if (!activeChapter || readingMode === "continuous") return;
    const currentIndex = Math.max(0, activePages.findIndex((page) => page.id === currentPageId));
    const targetIndex = movePagedIndex(
      currentIndex,
      delta,
      activePages.length,
      readingMode === "horizontal" ? spreadMode : "single",
      readingMode === "horizontal" && firstPageAlone,
    );
    setActivePage(activeChapter, activePages[targetIndex]);
  }, [activeChapter, activePages, currentPageId, firstPageAlone, readingMode, setActivePage, spreadMode]);

  const navigateChapter = useCallback(async (direction: ReaderDirection) => {
    const before = feed.getState();
    const chapter = before.chaptersById[currentChapterId];
    if (!chapter) return;
    await progressController.flush();
    let targetId = getAdjacentChapterId(chapter, direction);
    const canLoad = direction === "previous" ? before.hasMorePrevious : before.hasMoreNext;
    if ((!targetId || !before.chaptersById[targetId]) && canLoad) {
      const request = direction === "previous" ? feed.loadPrevious() : feed.loadNext();
      if (request) await request.catch(() => undefined);
    }
    let nextState = feed.getState();
    targetId ??= getAdjacentChapterId(nextState.chaptersById[currentChapterId] ?? chapter, direction);
    targetId ??= getLoadedAdjacentChapterId(currentChapterId, nextState.orderedChapterIds, direction);
    if (!targetId) return;
    if (!nextState.chaptersById[targetId]) {
      await feed.loadInitial(seriesSlug, targetId).catch(() => undefined);
      nextState = feed.getState();
    }
    const target = nextState.chaptersById[targetId];
    if (target) setActivePage(target, target.pages[0]);
  }, [currentChapterId, feed, progressController, seriesSlug, setActivePage]);

  useEffect(() => {
    const requestedChapter = feedState.chaptersById[requestedChapterId];
    if (resumeStarted.current || authInitializing || !requestedChapter || feedState.loadingInitial) return;
    resumeStarted.current = true;
    let active = true;
    void (async () => {
      const server = user ? await loadServerProgress(seriesSlug, requestedChapterId) : null;
      const local = loadLocalProgress(seriesSlug, requestedChapterId);
      const selected = chooseNewestProgress(server, local, seriesSlug, requestedChapterId);
      const resumed = selected ? clampProgress(selected, requestedChapter.pages.length) : null;
      const preferences = loadReaderPreferences(seriesSlug);
      if (!active) return;
      const page = resumed?.page ?? 1;
      const pageData = requestedChapter.pages.find((item) => item.pageNumber === page)
        ?? requestedChapter.pages[Math.max(0, page - 1)];
      const mode = preferences?.mode ?? local?.readingMode ?? DEFAULT_READER_PREFERENCES.mode;
      startTransition(() => {
        setCurrentChapterId(requestedChapterId);
        setCurrentPage(pageData?.pageNumber ?? 1);
        setCurrentPageId(pageData?.id ?? null);
        setCurrentScrollRatio(resumed?.scrollRatio ?? 0);
        setReadingMode(mode);
        setSpreadMode(preferences?.spreadMode ?? DEFAULT_READER_PREFERENCES.spreadMode);
        setZoom(preferences?.zoom ?? local?.zoom ?? DEFAULT_READER_PREFERENCES.zoom);
        setFitWidth(preferences?.fitWidth ?? DEFAULT_READER_PREFERENCES.fitWidth);
        setReadingDirection(preferences?.direction ?? DEFAULT_READER_PREFERENCES.direction);
        setFirstPageAlone(preferences?.firstPageAlone ?? DEFAULT_READER_PREFERENCES.firstPageAlone);
        setResumeReady(true);
      });
      feed.setVisibleChapterId(requestedChapterId);
    })();
    return () => { active = false; };
  }, [authInitializing, feed, feedState.chaptersById, feedState.loadingInitial, requestedChapterId, seriesSlug, user]);

  useEffect(() => {
    if (!resumeReady) return;
    saveReaderPreferences(seriesSlug, {
      mode: readingMode,
      spreadMode,
      zoom,
      fitWidth,
      direction: readingDirection,
      firstPageAlone,
    });
  }, [firstPageAlone, fitWidth, readingDirection, readingMode, resumeReady, seriesSlug, spreadMode, zoom]);

  useEffect(() => {
    if (!resumeReady) return;
    urlSynchronizer.update(currentChapterId);
  }, [currentChapterId, resumeReady, urlSynchronizer]);

  useEffect(() => {
    if (!resumeReady || !activeChapter || !currentPageId) return;
    progressController.update({
      seriesSlug,
      chapterId: activeChapter.id,
      page: currentPage,
      scrollRatio: currentScrollRatio,
      updatedAt: Date.now(),
    });
  }, [activeChapter, currentPage, currentPageId, currentScrollRatio, progressController, resumeReady, seriesSlug]);

  useEffect(() => {
    const removeProgressListeners = attachProgressFlushListeners(progressController);
    return () => {
      removeProgressListeners();
      urlSynchronizer.dispose();
    };
  }, [progressController, urlSynchronizer]);

  useEffect(() => {
    const activeChapterId = activeChapter?.id;
    if (!activeChapterId) return;
    feed.setChapterProtected(activeChapterId, true);
    return () => feed.setChapterProtected(activeChapterId, false);
  }, [activeChapter, feed]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isReaderInputTarget(event.target)) return;
      const action = getReaderKeyboardAction(event.key, readingMode, readingDirection);
      if (!action) return;
      if (event.key === " ") event.preventDefault();
      if (action === "previous-chapter") void navigateChapter("previous");
      if (action === "next-chapter") void navigateChapter("next");
      if (action === "previous-page") movePage(-1);
      if (action === "next-page") movePage(1);
      if (action === "first-page") goToPage(1);
      if (action === "last-page") goToPage(activePages.at(-1)?.pageNumber ?? 1);
      if (action === "zoom-in") setZoom((value) => clampReaderZoom(value + READER_ZOOM_STEP));
      if (action === "zoom-out") setZoom((value) => clampReaderZoom(value - READER_ZOOM_STEP));
      if (action === "reset-view") { setZoom(100); setFitWidth(false); }
      if (action === "toggle-fit") setFitWidth((value) => !value);
      if (action === "toggle-help") setShowHint((value) => !value);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activePages, goToPage, movePage, navigateChapter, readingDirection, readingMode]);

  const onContinuousRangeChanged = useCallback((range: { startIndex: number; endIndex: number }) => {
    const visible = selectViewportCenterPage(feedState.items, range, feedState.firstItemIndex, feedState.chaptersById);
    if (!visible) return;
    const chapter = feedState.chaptersById[visible.chapterId];
    const page = chapter?.pages.find((item) => item.id === visible.pageId);
    if (chapter && page) setActivePage(chapter, page);
  }, [feedState.chaptersById, feedState.firstItemIndex, feedState.items, setActivePage]);

  const handleModeChange = useCallback((mode: ReadingMode) => {
    if (mode === readingMode) return;
    if (activeChapter) feed.setVisibleChapterId(activeChapter.id);
    if (mode === "continuous") nextRequestReason.current = "modeTransition";
    setReadingMode(mode);
  }, [activeChapter, feed, readingMode]);

  if (feedState.loadingInitial || !resumeReady) {
    return <div className="reader-loading"><div className="spinner" /><p>Loading chapter...</p></div>;
  }
  if (!activeChapter || activePages.length === 0) {
    return (
      <div className="reader-error">
        <p>{feedState.error || (activeChapter ? "No verified pages available" : "Chapter not found")}</p>
        {feedState.retryState && <button className="btn" onClick={() => void feed.retry()?.catch(() => undefined)}>Retry</button>}
        <button className="btn" onClick={() => router.push(`/series/${seriesSlug}`)}>Go to Series</button>
      </div>
    );
  }

  const progress = Math.round((currentPage / activePages.length) * 100);
  const continuousInitialIndex = feedState.items.findIndex((item) =>
    item.kind === "page" && item.chapterId === currentChapterId && item.pageId === currentPageId,
  );
  const hasPreviousChapter = !!activeChapter.previousChapterId || feedState.hasMorePrevious;
  const hasNextChapter = !!activeChapter.nextChapterId || feedState.hasMoreNext;
  const diagnostics = feed.getDiagnostics(
    feedState.items.length,
    feedState.items.filter((item) => item.kind === "page").length,
  );
  return (
    <div className={`reader ${readingMode}`} data-reading-mode={readingMode}>
      <div className="progress-wrap"><div className="progress-bar" style={{ width: `${progress}%` }} /></div>
      <ReaderToolbar
        title={activeChapter.title ?? `Chapter ${activeChapter.number}`}
        page={currentPage}
        totalPages={activePages.length}
        mode={readingMode}
        spreadMode={spreadMode}
        zoom={zoom}
        fitWidth={fitWidth}
        direction={readingDirection}
        progressStatus={progressStatus}
        hasPreviousChapter={hasPreviousChapter}
        hasNextChapter={hasNextChapter}
        onBack={() => router.push(`/series/${seriesSlug}`)}
        onModeChange={handleModeChange}
        onSpreadModeChange={setSpreadMode}
        onZoomChange={setZoom}
        onFitWidthChange={setFitWidth}
        onDirectionChange={setReadingDirection}
        onPreviousChapter={() => { void navigateChapter("previous"); }}
        onNextChapter={() => { void navigateChapter("next"); }}
        onPageChange={goToPage}
        onPreviousPage={() => movePage(-1)}
        onNextPage={() => movePage(1)}
      />
      <div
        className="reader-viewport"
        onClick={(event) => {
          if (readingMode !== "horizontal" || event.target !== event.currentTarget) return;
          const rect = event.currentTarget.getBoundingClientRect();
          const clickedLeft = event.clientX - rect.left < rect.width / 2;
          const delta = clickedLeft === (readingDirection === "ltr") ? -1 : 1;
          movePage(delta);
        }}
      >
        {readingMode === "continuous" ? (
          <ContinuousReader
            items={feedState.items}
            firstItemIndex={feedState.firstItemIndex}
            hasMoreNext={feedState.hasMoreNext}
            hasMorePrevious={feedState.hasMorePrevious}
            loadingNext={feedState.loadingNext}
            loadingPrevious={feedState.loadingPrevious}
            error={feedState.error}
            zoom={zoom}
            fitWidth={fitWidth}
            onLoadNext={loadNext}
            onLoadPrevious={loadPrevious}
            onRetry={retryFeed}
            onRangeChanged={onContinuousRangeChanged}
            initialTopMostItemIndex={continuousInitialIndex < 0 ? undefined : continuousInitialIndex}
            registerPageCleanup={registerPageCleanup}
          />
        ) : (
          <PagedReader
            chapter={activeChapter}
            currentPageId={currentPageId}
            mode={readingMode}
            spreadMode={spreadMode}
            direction={readingDirection}
            firstPageAlone={firstPageAlone}
            zoom={zoom}
            fitWidth={fitWidth}
            onVisiblePageChange={(page) => setActivePage(activeChapter, page)}
          />
        )}
      </div>
      <KeyboardHint visible={showHint} />
      <button className="fab-home" onClick={() => router.push("/")} title="Go Home">InfinityScan</button>
      <div className="reader-preference-extra">
        {readingMode === "horizontal" && spreadMode === "spread" && (
          <label><input type="checkbox" checked={firstPageAlone} onChange={(event) => setFirstPageAlone(event.target.checked)} /> First page alone</label>
        )}
      </div>
      {diagnostics && (
        <output
          data-testid="reader-diagnostics"
          hidden
          data-visible-chapter-id={currentChapterId}
          data-visible-page-id={currentPageId ?? ""}
          data-visible-page-number={currentPage}
          data-retained-chapters={diagnostics.retainedChapterCount}
          data-retained-pages={diagnostics.retainedPageCount}
          data-rendered-items={diagnostics.renderedItemCount}
          data-rendered-pages={diagnostics.renderedPageCount}
          data-active-next-requests={diagnostics.activeNextRequests}
          data-active-previous-requests={diagnostics.activePreviousRequests}
          data-has-more-next={feedState.hasMoreNext ? "1" : "0"}
          data-has-more-previous={feedState.hasMorePrevious ? "1" : "0"}
        />
      )}
    </div>
  );
}
