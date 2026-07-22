"use client";

import { startTransition, use, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Virtuoso, type ListRange } from "react-virtuoso";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/auth";
import { fetchReaderChunk } from "@/features/reader/api";
import { ContinuousReader } from "@/features/reader/components/ContinuousReader";
import { ReaderFeedController } from "@/features/reader/feed";
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
import type { ReaderItem, ReaderPage, ReadingMode } from "@/features/reader/types";

type SpreadMode = "single" | "spread";

const ZOOM_MIN = 40;
const ZOOM_MAX = 160;
const ZOOM_STEP = 10;

type RenderPage = Extract<ReaderItem, { kind: "page" }>;

interface TopBarProps {
  title: string;
  page: number;
  totalPages: number;
  readingMode: ReadingMode;
  spreadMode: SpreadMode;
  zoom: number;
  hasPrev: boolean;
  hasNext: boolean;
  onBack: () => void;
  onModeChange: (mode: ReadingMode) => void;
  onSpreadChange: (mode: SpreadMode) => void;
  onZoomChange: (zoom: number) => void;
  onPrev: () => void;
  onNext: () => void;
}

function TopBar(props: TopBarProps) {
  return (
    <div className="reader-top-bar">
      <button className="btn ghost small" onClick={props.onBack}>← Back</button>
      <span className="chapter-title">{props.title}</span>
      <span className="page-indicator">{props.page} / {props.totalPages}</span>
      <div className="control-group">
        {(["vertical", "horizontal", "scroll"] as ReadingMode[]).map((mode) => (
          <button
            key={mode}
            className={`btn tiny ${props.readingMode === mode ? "active" : "ghost"}`}
            onClick={() => props.onModeChange(mode)}
            title={mode}
          >
            {mode === "vertical" ? "↓" : mode === "horizontal" ? "→" : "☰"}
          </button>
        ))}
        {(props.readingMode === "horizontal" || props.readingMode === "scroll") && (
          <>
            <button className={`btn tiny ${props.spreadMode === "single" ? "active" : "ghost"}`} onClick={() => props.onSpreadChange("single")}>1</button>
            <button className={`btn tiny ${props.spreadMode === "spread" ? "active" : "ghost"}`} onClick={() => props.onSpreadChange("spread")}>2</button>
          </>
        )}
      </div>
      <div className="control-group zoom-controls">
        <button className="btn ghost tiny" onClick={() => props.onZoomChange(Math.max(ZOOM_MIN, props.zoom - ZOOM_STEP))}>−</button>
        <span className="zoom-value">{props.zoom}%</span>
        <button className="btn ghost tiny" onClick={() => props.onZoomChange(Math.min(ZOOM_MAX, props.zoom + ZOOM_STEP))}>+</button>
      </div>
      <div className="control-group">
        {props.hasPrev && <button className="btn ghost tiny" onClick={props.onPrev}>← Prev</button>}
        {props.hasNext && <button className="btn ghost tiny" onClick={props.onNext}>Next →</button>}
      </div>
    </div>
  );
}

interface PageImageProps {
  page: ReaderPage;
  zoom: number;
  fitWidth: boolean;
}

function PageImage({ page, zoom, fitWidth }: PageImageProps) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  if (failed) {
    return <div className="page-error">Failed to load page {page.pageNumber}</div>;
  }
  return (
    <div className="page">
      <img
        src={page.mediaPath}
        alt={`Page ${page.pageNumber}`}
        loading="lazy"
        onLoad={() => setLoaded(true)}
        onError={() => setFailed(true)}
        style={{
          transform: fitWidth ? undefined : `scale(${zoom / 100})`,
          transformOrigin: "top center",
          maxWidth: fitWidth ? "100%" : `${zoom}%`,
          width: fitWidth ? "100%" : "auto",
          opacity: loaded ? 1 : 0.3,
        }}
      />
    </div>
  );
}

function Spread({ left, right }: { left: ReaderPage; right?: ReaderPage }) {
  return (
    <div className="spread-container">
      <div className="spread-page"><img src={left.mediaPath} alt={`Page ${left.pageNumber}`} style={{ height: "calc(100vh - 120px)", width: "auto", maxWidth: "48vw" }} /></div>
      {right && <div className="spread-page"><img src={right.mediaPath} alt={`Page ${right.pageNumber}`} style={{ height: "calc(100vh - 120px)", width: "auto", maxWidth: "48vw" }} /></div>}
    </div>
  );
}

function KeyboardHint({ visible }: { visible: boolean }) {
  if (!visible) return null;
  return <div className="keyboard-hint"><span>←→ Pages</span><span>Space Next</span><span>[ ] Chapters</span><span>? Help</span></div>;
}

interface VirtualizedPagesProps {
  pages: RenderPage[];
  firstItemIndex: number;
  zoom: number;
  fitWidth: boolean;
  onEndReached: () => void;
  onStartReached: () => void;
  onRangeChanged: (range: ListRange) => void;
  initialTopMostItemIndex?: number;
}

function VirtualizedPages(props: VirtualizedPagesProps) {
  return (
    <Virtuoso
      data={props.pages}
      firstItemIndex={props.firstItemIndex}
      initialTopMostItemIndex={props.initialTopMostItemIndex}
      increaseViewportBy={{ top: 700, bottom: 1200 }}
      computeItemKey={(_, page) => page.key}
      itemContent={(_, page) => <PageImage page={{ id: page.pageId, pageNumber: page.pageNumber, mediaPath: page.mediaPath, width: page.width, height: page.height, aspectRatio: page.aspectRatio }} zoom={props.zoom} fitWidth={props.fitWidth} />}
      endReached={props.onEndReached}
      startReached={props.onStartReached}
      rangeChanged={props.onRangeChanged}
      style={{ height: "100%", paddingTop: 56, paddingBottom: 60 }}
    />
  );
}

interface ReaderProps {
  params: Promise<{ slug: string; chapter: string }>;
}

export default function EnhancedReader({ params }: ReaderProps) {
  const { slug: seriesSlug, chapter: chapterId } = use(params);
  const router = useRouter();
  const user = useAuthStore((state) => state.user);
  const authInitializing = useAuthStore((state) => state.isInitializing);
  const [requestedChapterId] = useState(chapterId);
  const [feed] = useState(() => new ReaderFeedController(fetchReaderChunk));
  const feedState = useSyncExternalStore(feed.subscribe, feed.getState, feed.getState);
  const [currentChapterId, setCurrentChapterId] = useState(requestedChapterId);
  const [currentPage, setCurrentPage] = useState(1);
  const [currentPageId, setCurrentPageId] = useState<string | null>(null);
  const [currentScrollRatio, setCurrentScrollRatio] = useState(0);
  const [readingMode, setReadingMode] = useState<ReadingMode>("vertical");
  const [spreadMode, setSpreadMode] = useState<SpreadMode>("single");
  const [zoom, setZoom] = useState(100);
  const [fitWidth, setFitWidth] = useState(false);
  const [showHint, setShowHint] = useState(false);
  const [resumeReady, setResumeReady] = useState(false);
  const [progressStatus, setProgressStatus] = useState<ProgressStatus>("saved");
  const resumeStarted = useRef(false);
  const [urlSynchronizer] = useState(
    () => new ReaderUrlSynchronizer(router, seriesSlug, requestedChapterId),
  );
  const [progressController] = useState(() => new ReaderProgressController({
    seriesSlug,
    authenticated: !!useAuthStore.getState().user,
    onStatus: setProgressStatus,
  }));

  useEffect(() => {
    void feed.loadInitial(seriesSlug, requestedChapterId).catch(() => undefined);
    return () => feed.dispose();
  }, [feed, requestedChapterId, seriesSlug]);

  useEffect(() => {
    progressController.setAuthenticated(!!user);
  }, [progressController, user]);

  const loadNext = useCallback(() => feed.loadNext(), [feed]);
  const loadPrevious = useCallback(() => feed.loadPrevious(), [feed]);
  const retryFeed = useCallback(() => feed.retry(), [feed]);
  const registerPageCleanup = useCallback(
    (pageId: string, cleanup: () => void) => feed.registerPageCleanup(pageId, cleanup),
    [feed],
  );

  const activeChapter = feedState.chaptersById[currentChapterId]
    ?? (feedState.orderedChapterIds[0] ? feedState.chaptersById[feedState.orderedChapterIds[0]] : undefined);
  const activePages = useMemo(() => activeChapter?.pages ?? [], [activeChapter]);

  const goToPage = useCallback((page: number) => {
    const nextPage = Math.min(Math.max(1, page), Math.max(1, activePages.length));
    const pageData = activePages.find((item) => item.pageNumber === nextPage) ?? activePages[nextPage - 1];
    setCurrentPage(nextPage);
    setCurrentPageId(pageData?.id ?? null);
    setCurrentScrollRatio(activePages.length <= 1 ? 0 : Math.min(1, Math.max(0, (nextPage - 1) / (activePages.length - 1))));
  }, [activePages]);

  const navigateToChapter = useCallback((id: string | null) => {
    if (id) router.push(`/reader/${encodeURIComponent(seriesSlug)}/${encodeURIComponent(id)}`);
  }, [router, seriesSlug]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((event.target as HTMLElement).tagName)) return;
      if (!activeChapter) return;
      if (event.key === "ArrowLeft" && readingMode === "horizontal") goToPage(currentPage - (spreadMode === "spread" ? 2 : 1));
      if (event.key === "ArrowRight" && readingMode === "horizontal") goToPage(currentPage + (spreadMode === "spread" ? 2 : 1));
      if (event.key === " " && readingMode !== "scroll") { event.preventDefault(); goToPage(currentPage + 1); }
      if (event.key === "[") navigateToChapter(activeChapter.previousChapterId);
      if (event.key === "]") navigateToChapter(activeChapter.nextChapterId);
      if (event.key === "Home") goToPage(1);
      if (event.key === "End") goToPage(activePages.length);
      if (event.key === "?") setShowHint((visible) => !visible);
      if (event.key === "+" || event.key === "=") setZoom((value) => Math.min(ZOOM_MAX, value + ZOOM_STEP));
      if (event.key === "-") setZoom((value) => Math.max(ZOOM_MIN, value - ZOOM_STEP));
      if (event.key === "0") { setZoom(100); setFitWidth(false); }
      if (event.key.toLowerCase() === "f") setFitWidth((value) => !value);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activeChapter, activePages.length, currentPage, goToPage, navigateToChapter, readingMode, spreadMode]);

  useEffect(() => {
    const activeChapterId = activeChapter?.id;
    if (!activeChapterId) return;
    feed.setChapterProtected(activeChapterId, true);
    return () => feed.setChapterProtected(activeChapterId, false);
  }, [activeChapter, feed]);

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
      if (!active) return;
      const page = resumed?.page ?? 1;
      const pageData = requestedChapter.pages.find((item) => item.pageNumber === page)
        ?? requestedChapter.pages[Math.max(0, page - 1)];
      startTransition(() => {
        setCurrentChapterId(requestedChapterId);
        setCurrentPage(pageData?.pageNumber ?? 1);
        setCurrentPageId(pageData?.id ?? null);
        setCurrentScrollRatio(resumed?.scrollRatio ?? 0);
        if (local?.readingMode) setReadingMode(local.readingMode);
        if (local?.zoom) setZoom(local.zoom);
        setResumeReady(true);
      });
      feed.setVisibleChapterId(requestedChapterId);
    })();
    return () => { active = false; };
  }, [authInitializing, feed, feedState.chaptersById, feedState.loadingInitial, requestedChapterId, seriesSlug, user]);

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
      readingMode,
      zoom,
    });
  }, [activeChapter, currentPage, currentPageId, currentScrollRatio, progressController, readingMode, resumeReady, seriesSlug, zoom]);

  useEffect(() => {
    const removeProgressListeners = attachProgressFlushListeners(progressController);
    return () => {
      removeProgressListeners();
      urlSynchronizer.dispose();
    };
  }, [progressController, urlSynchronizer]);

  const onRangeChanged = useCallback((range: ListRange) => {
    const visible = selectViewportCenterPage(
      feedState.items,
      range,
      feedState.firstItemIndex,
      feedState.chaptersById,
    );
    if (!visible) return;
    setCurrentChapterId(visible.chapterId);
    setCurrentPageId(visible.pageId);
    setCurrentPage(visible.pageNumber);
    setCurrentScrollRatio(visible.scrollRatio);
    feed.setVisibleChapterId(visible.chapterId);
  }, [feed, feedState.chaptersById, feedState.firstItemIndex, feedState.items]);

  if (feedState.loadingInitial || !resumeReady) return <div className="reader-loading"><div className="spinner" /><p>Loading chapter...</p></div>;
  if (!activeChapter || activePages.length === 0) {
    return (
      <div className="reader-error">
        <p>{feedState.error || (activeChapter ? "No verified pages available" : "Chapter not found")}</p>
        {feedState.retryState && <button className="btn" onClick={() => void feed.retry()?.catch(() => undefined)}>Retry</button>}
        <button className="btn" onClick={() => router.push(`/series/${seriesSlug}`)}>Go to Series</button>
      </div>
    );
  }

  const progress = activePages.length ? Math.round((currentPage / activePages.length) * 100) : 0;
  const continuousInitialIndex = feedState.items.findIndex((item) =>
    item.kind === "page" && item.chapterId === currentChapterId && item.pageId === currentPageId,
  );
  const verticalInitialIndex = activePages.findIndex((page) => page.id === currentPageId);
  return (
    <div className={`reader ${readingMode}`}>
      <div className="progress-wrap"><div className="progress-bar" style={{ width: `${progress}%` }} /></div>
      <TopBar
        title={activeChapter.title ?? `Chapter ${activeChapter.number}`}
        page={currentPage}
        totalPages={activePages.length}
        readingMode={readingMode}
        spreadMode={spreadMode}
        zoom={zoom}
        hasPrev={!!activeChapter.previousChapterId || feedState.hasMorePrevious}
        hasNext={!!activeChapter.nextChapterId || feedState.hasMoreNext}
        onBack={() => router.push(`/series/${seriesSlug}`)}
        onModeChange={setReadingMode}
        onSpreadChange={setSpreadMode}
        onZoomChange={setZoom}
        onPrev={() => navigateToChapter(activeChapter.previousChapterId)}
        onNext={() => navigateToChapter(activeChapter.nextChapterId)}
      />
      <div
        className="reader-viewport"
        onClick={(event) => {
          if (readingMode !== "horizontal" || event.target !== event.currentTarget) return;
          const rect = event.currentTarget.getBoundingClientRect();
          goToPage(currentPage + (event.clientX - rect.left < rect.width / 2 ? -1 : 1));
        }}
      >
        {readingMode === "scroll" ? (
          <ContinuousReader
            items={feedState.items}
            firstItemIndex={feedState.firstItemIndex}
            hasMoreNext={feedState.hasMoreNext}
            hasMorePrevious={feedState.hasMorePrevious}
            loadingNext={feedState.loadingNext}
            loadingPrevious={feedState.loadingPrevious}
            error={feedState.error}
            zoom={zoom}
            fitWidth
            onLoadNext={loadNext}
            onLoadPrevious={loadPrevious}
            onRetry={retryFeed}
            onRangeChanged={onRangeChanged}
            initialTopMostItemIndex={continuousInitialIndex < 0
              ? undefined
              : feedState.firstItemIndex + continuousInitialIndex}
            registerPageCleanup={registerPageCleanup}
          />
        ) : readingMode === "vertical" ? (
          <VirtualizedPages
            pages={activePages.map((page) => ({
              kind: "page" as const,
              key: `page:${page.id}`,
              chapterId: activeChapter.id,
              chapterNumber: activeChapter.number,
              pageId: page.id,
              pageNumber: page.pageNumber,
              mediaPath: page.mediaPath,
              width: page.width,
              height: page.height,
              aspectRatio: page.aspectRatio,
            }))}
            firstItemIndex={0}
            zoom={zoom}
            fitWidth={fitWidth}
            onEndReached={() => undefined}
            onStartReached={() => undefined}
            initialTopMostItemIndex={verticalInitialIndex < 0 ? undefined : verticalInitialIndex}
            onRangeChanged={(range) => {
              const items = activePages.map((page) => ({
                kind: "page" as const,
                key: `page:${page.id}`,
                chapterId: activeChapter.id,
                chapterNumber: activeChapter.number,
                pageId: page.id,
                pageNumber: page.pageNumber,
                mediaPath: page.mediaPath,
                width: page.width,
                height: page.height,
                aspectRatio: page.aspectRatio,
              }));
              const visible = selectViewportCenterPage(items, range, 0, feedState.chaptersById);
              if (!visible) return;
              setCurrentPage(visible.pageNumber);
              setCurrentPageId(visible.pageId);
              setCurrentScrollRatio(visible.scrollRatio);
            }}
          />
        ) : spreadMode === "spread" ? (
          <Spread left={activePages[currentPage - 1]} right={activePages[currentPage]} />
        ) : (
          <div className="pages-horizontal">
            <PageImage page={activePages[currentPage - 1]} zoom={zoom} fitWidth={fitWidth} />
          </div>
        )}
      </div>
      <div className="reader-controls">
        <span>{activeChapter.title ?? `Chapter ${activeChapter.number}`}</span>
        <span className="reader-save-status" role="status">{progressStatus}</span>
        <input
          className="page-input"
          type="number"
          min={1}
          max={activePages.length}
          value={currentPage}
          onChange={(event) => goToPage(Number.parseInt(event.target.value, 10) || 1)}
        />
        <button className="btn ghost small" onClick={() => goToPage(currentPage - 1)}>Prev page</button>
        <button className="btn ghost small" onClick={() => goToPage(currentPage + 1)}>Next page</button>
        <button className="btn ghost small" onClick={() => { setZoom(100); setFitWidth(false); }}>Reset</button>
      </div>
      <KeyboardHint visible={showHint} />
      <button className="fab-home" onClick={() => router.push("/")} title="Go Home">∞</button>
    </div>
  );
}
