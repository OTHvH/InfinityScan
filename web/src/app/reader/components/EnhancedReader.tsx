"use client";

import { startTransition, use, useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { Virtuoso, type ListRange } from "react-virtuoso";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/auth";
import { fetchReaderChunk } from "@/features/reader/api";
import { ContinuousReader } from "@/features/reader/components/ContinuousReader";
import { ReaderFeedController } from "@/features/reader/feed";
import { loadLocalProgress, loadServerProgress, saveLocalProgress, saveServerProgress } from "@/features/reader/progress";
import type { ReaderItem, ReaderPage, ReadingMode } from "@/features/reader/types";

type SpreadMode = "single" | "spread";

const PROGRESS_SAVE_INTERVAL = 3000;
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
}

function VirtualizedPages(props: VirtualizedPagesProps) {
  return (
    <Virtuoso
      data={props.pages}
      firstItemIndex={props.firstItemIndex}
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
  const [feed] = useState(() => new ReaderFeedController(fetchReaderChunk));
  const feedState = useSyncExternalStore(feed.subscribe, feed.getState, feed.getState);
  const [currentChapterId, setCurrentChapterId] = useState(chapterId);
  const [currentPage, setCurrentPage] = useState(1);
  const [readingMode, setReadingMode] = useState<ReadingMode>("vertical");
  const [spreadMode, setSpreadMode] = useState<SpreadMode>("single");
  const [zoom, setZoom] = useState(100);
  const [fitWidth, setFitWidth] = useState(false);
  const [showHint, setShowHint] = useState(false);

  useEffect(() => {
    startTransition(() => {
      setCurrentChapterId(chapterId);
      setCurrentPage(1);
    });
    void feed.loadInitial(seriesSlug, chapterId).catch(() => undefined);
    return () => feed.dispose();
  }, [chapterId, feed, seriesSlug]);

  const loadNext = useCallback(() => feed.loadNext(), [feed]);
  const loadPrevious = useCallback(() => feed.loadPrevious(), [feed]);
  const retryFeed = useCallback(() => feed.retry(), [feed]);

  const activeChapter = feedState.chaptersById[currentChapterId]
    ?? (feedState.orderedChapterIds[0] ? feedState.chaptersById[feedState.orderedChapterIds[0]] : undefined);
  const activePages = activeChapter?.pages ?? [];

  const goToPage = useCallback((page: number) => {
    setCurrentPage(Math.min(Math.max(1, page), Math.max(1, activePages.length)));
  }, [activePages.length]);

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
    if (!activeChapter) return;
    const saved = loadLocalProgress(seriesSlug, activeChapter.id);
    if (saved) {
      startTransition(() => {
        setCurrentPage(Math.min(saved.page, Math.max(1, activePages.length)));
        if (saved.readingMode) setReadingMode(saved.readingMode);
        if (saved.zoom) setZoom(saved.zoom);
      });
    }
    let active = true;
    if (useAuthStore.getState().user) {
      void loadServerProgress(seriesSlug, activeChapter.id).then((serverPage) => {
        if (active && serverPage != null) setCurrentPage(Math.min(serverPage, Math.max(1, activePages.length)));
      });
    }
    return () => { active = false; };
  }, [activeChapter, activePages.length, seriesSlug]);

  useEffect(() => {
    if (!activeChapter) return;
    const save = () => {
      saveLocalProgress(seriesSlug, activeChapter.id, { page: currentPage, readingMode, zoom });
      if (useAuthStore.getState().user) void saveServerProgress(seriesSlug, activeChapter.id, currentPage);
    };
    const timer = window.setInterval(save, PROGRESS_SAVE_INTERVAL);
    return () => window.clearInterval(timer);
  }, [activeChapter, currentPage, readingMode, seriesSlug, zoom]);

  const onRangeChanged = useCallback((range: ListRange) => {
    const item = feedState.items[range.startIndex];
    if (!item) return;
    setCurrentChapterId(item.chapterId);
    feed.setVisibleChapterId(item.chapterId);
    if (item.kind === "page") setCurrentPage(item.pageNumber);
  }, [feed, feedState.items]);

  if (feedState.loadingInitial) return <div className="reader-loading"><div className="spinner" /><p>Loading chapter...</p></div>;
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
            onRangeChanged={(range) => {
              const page = activePages[range.startIndex];
              if (page) setCurrentPage(page.pageNumber);
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
