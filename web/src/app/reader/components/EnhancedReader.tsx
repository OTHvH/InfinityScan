"use client";

import React, { use, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

// ─── Types ────────────────────────────────────────────────────────────────────

interface Page {
  page_number: number;
  url: string;
}

interface Chapter {
  uuid: string;
  name: string;
  index: number;
  pages: Page[];
  prev_chapter_uuid: string | null;
  next_chapter_uuid: string | null;
  chapter_number: number;
}

type ReadingMode = "vertical" | "horizontal" | "scroll";
type SpreadMode = "single" | "spread";

// ─── Constants ────────────────────────────────────────────────────────────────

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const PRELOAD_THRESHOLD = 3;
const PROGRESS_SAVE_INTERVAL = 3000;
const ZOOM_MIN = 40;
const ZOOM_MAX = 160;
const ZOOM_STEP = 10;

// ─── Helper Functions ────────────────────────────────────────────────────────

function getStorageKey(seriesSlug: string, chapterUuid: string, mode: string): string {
  return `infinityscan_${mode}_${seriesSlug}_${chapterUuid}`;
}

function saveProgress(seriesSlug: string, chapterUuid: string, page: number, readingMode: ReadingMode, zoom: number) {
  localStorage.setItem(
    getStorageKey(seriesSlug, chapterUuid, "progress"),
    JSON.stringify({ page, readingMode, zoom, timestamp: Date.now() })
  );
}

function loadProgress(seriesSlug: string, chapterUuid: string): { page: number; readingMode: ReadingMode; zoom: number } | null {
  const data = localStorage.getItem(getStorageKey(seriesSlug, chapterUuid, "progress"));
  if (!data) return null;
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
}

// ─── Components ────────────────────────────────────────────────────────────────

interface TopBarProps {
  title: string;
  currentPage: number;
  totalPages: number;
  readingMode: ReadingMode;
  spreadMode: SpreadMode;
  zoom: number;
  hasPrev: boolean;
  hasNext: boolean;
  onBack: () => void;
  onModeChange: (mode: ReadingMode) => void;
  onSpreadChange: (spread: SpreadMode) => void;
  onZoomChange: (zoom: number) => void;
  onPrevChapter: () => void;
  onNextChapter: () => void;
}

function TopBar({ 
  title, 
  currentPage, 
  totalPages,
  readingMode,
  spreadMode,
  zoom,
  hasPrev,
  hasNext,
  onBack,
  onModeChange,
  onSpreadChange,
  onZoomChange,
  onPrevChapter,
  onNextChapter,
}: TopBarProps) {
  return (
    <div className="reader-top-bar" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', padding: '8px 12px' }}>
      <button className="btn ghost small" onClick={onBack}>← Back</button>
      <span className="chapter-title">{title}</span>
      <span className="page-indicator">{currentPage} / {totalPages}</span>
      
      {/* Mode controls in top bar */}
      <div className="control-group" style={{ borderRight: 'none', marginLeft: 'auto' }}>
        <button
          className={`btn tiny ${readingMode === "vertical" ? "active" : "ghost"}`}
          onClick={() => onModeChange("vertical")}
          title="Vertical"
        >
          ↓
        </button>
        <button
          className={`btn tiny ${readingMode === "horizontal" ? "active" : "ghost"}`}
          onClick={() => onModeChange("horizontal")}
          title="Horizontal"
        >
          →
        </button>
        <button
          className={`btn tiny ${readingMode === "scroll" ? "active" : "ghost"}`}
          onClick={() => onModeChange("scroll")}
          title="Scroll"
        >
          ☰
        </button>
        {(readingMode === "horizontal" || readingMode === "scroll") && (
          <>
            <button
              className={`btn tiny ${spreadMode === "single" ? "active" : "ghost"}`}
              onClick={() => onSpreadChange("single")}
              title="Single"
            >
              1
            </button>
            <button
              className={`btn tiny ${spreadMode === "spread" ? "active" : "ghost"}`}
              onClick={() => onSpreadChange("spread")}
              title="Spread"
            >
              2
            </button>
          </>
        )}
      </div>
      
      {/* Zoom controls */}
      <div className="control-group zoom-controls" style={{ borderRight: 'none' }}>
        <button
          className="btn ghost tiny"
          onClick={() => onZoomChange(Math.max(40, zoom - 10))}
          title="Zoom -"
        >
          −
        </button>
        <span className="zoom-value" style={{ fontSize: '11px', minWidth: '32px' }}>{zoom}%</span>
        <button
          className="btn ghost tiny"
          onClick={() => onZoomChange(Math.min(160, zoom + 10))}
          title="Zoom +"
        >
          +
        </button>
      </div>
      
      {/* Chapter navigation */}
      <div className="control-group" style={{ borderRight: 'none' }}>
        {hasPrev && (
          <button className="btn ghost tiny" onClick={onPrevChapter}>
            ← Prev
          </button>
        )}
        {hasNext && (
          <button className="btn ghost tiny" onClick={onNextChapter}>
            Next →
          </button>
        )}
      </div>
    </div>
  );
}

interface BottomControlsProps {
  readingMode: ReadingMode;
  spreadMode: SpreadMode;
  zoom: number;
  currentPage: number;
  totalPages: number;
  hasPrev: boolean;
  hasNext: boolean;
  onModeChange: (mode: ReadingMode) => void;
  onSpreadChange: (spread: SpreadMode) => void;
  onZoomChange: (zoom: number) => void;
  onPageChange: (page: number) => void;
  onPrevChapter: () => void;
  onNextChapter: () => void;
}

function BottomControls({
  readingMode,
  spreadMode,
  zoom,
  currentPage,
  totalPages,
  hasPrev,
  hasNext,
  onModeChange,
  onSpreadChange,
  onZoomChange,
  onPageChange,
  onPrevChapter,
  onNextChapter,
}: BottomControlsProps) {
  const progress = Math.round((currentPage / totalPages) * 100);
  
  return (
    <>
      <div className="progress-wrap">
        <div className="progress-bar" style={{ width: `${progress}%` }} />
      </div>
      <div className="reader-controls">
        {/* Reading Mode */}
        <div className="control-group">
          <button
            className={`btn small ${readingMode === "vertical" ? "active" : "ghost"}`}
            onClick={() => onModeChange("vertical")}
            title="Vertical scroll"
          >
            ↓ Vertical
          </button>
          <button
            className={`btn small ${readingMode === "horizontal" ? "active" : "ghost"}`}
            onClick={() => onModeChange("horizontal")}
            title="Horizontal RTL"
          >
            → Horizontal
          </button>
          <button
            className={`btn small ${readingMode === "scroll" ? "active" : "ghost"}`}
            onClick={() => onModeChange("scroll")}
            title="Continuous scroll"
          >
            ☰ Scroll
          </button>
        </div>

        {/* Spread Mode */}
        {(readingMode === "horizontal" || readingMode === "scroll") && (
          <div className="control-group">
            <button
              className={`btn small ${spreadMode === "single" ? "active" : "ghost"}`}
              onClick={() => onSpreadChange("single")}
              title="Single page"
            >
              1
            </button>
            <button
              className={`btn small ${spreadMode === "spread" ? "active" : "ghost"}`}
              onClick={() => onSpreadChange("spread")}
              title="Book spread (2 pages)"
            >
              2
            </button>
          </div>
        )}

        {/* Zoom Controls */}
        <div className="control-group zoom-controls">
          <button
            className="btn ghost small"
            onClick={() => onZoomChange(Math.max(ZOOM_MIN, zoom - ZOOM_STEP))}
            disabled={zoom <= ZOOM_MIN}
          >
            −
          </button>
          <span className="zoom-value">{zoom}%</span>
          <button
            className="btn ghost small"
            onClick={() => onZoomChange(Math.min(ZOOM_MAX, zoom + ZOOM_STEP))}
            disabled={zoom >= ZOOM_MAX}
          >
            +
          </button>
          <button
            className="btn ghost small"
            onClick={() => onZoomChange(100)}
            title="Reset zoom"
          >
            ⟲
          </button>
          <button
            className="btn ghost small"
            onClick={() => onZoomChange(-1)}
            title="Fit width"
          >
            ↔
          </button>
        </div>

        {/* Page Navigation */}
        <div className="control-group">
          {hasPrev && (
            <button className="btn ghost small" onClick={onPrevChapter}>
              ← Prev
            </button>
          )}
          <input
            type="number"
            min={1}
            max={totalPages}
            value={currentPage}
            onChange={(e) => onPageChange(Math.min(Math.max(1, parseInt(e.target.value) || 1), totalPages))}
            className="page-input"
          />
          {hasNext && (
            <button className="btn ghost small" onClick={onNextChapter}>
              Next →
            </button>
          )}
        </div>
      </div>
    </>
  );
}

interface PageImageProps {
  page: Page;
  zoom: number;
  fitWidth: boolean;
}

function PageImage({ page, zoom, fitWidth }: PageImageProps) {
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);
  
  const style: React.CSSProperties = {
    transform: fitWidth ? undefined : `scale(${zoom / 100})`,
    transformOrigin: "top center",
    maxWidth: fitWidth ? "100%" : `${zoom}%`,
    width: fitWidth ? "100%" : "auto",
    transition: "transform 0.2s ease, opacity 0.2s ease",
    opacity: loaded ? 1 : 0.3,
  };
  
  if (error) {
    return (
      <div className="page-error">
        <span>Failed to load page {page.page_number}</span>
      </div>
    );
  }
  
  return (
    <div className="page">
      <img
        src={page.url}
        alt={`Page ${page.page_number}`}
        style={style}
        onLoad={() => setLoaded(true)}
        onError={() => setError(true)}
        loading="lazy"
      />
    </div>
  );
}

interface SpreadProps {
  leftPage: Page;
  rightPage: Page | null;
  zoom: number;
}

function Spread({ leftPage, rightPage, zoom }: SpreadProps) {
  return (
    <div className="spread-container" style={{ display: "flex", gap: 4 }}>
      <div className="spread-page">
        <img src={leftPage.url} alt={`Page ${leftPage.page_number}`} style={{ height: "calc(100vh - 120px)", width: "auto" }} />
      </div>
      {rightPage && (
        <div className="spread-page">
          <img src={rightPage.url} alt={`Page ${rightPage.page_number}`} style={{ height: "calc(100vh - 120px)", width: "auto" }} />
        </div>
      )}
    </div>
  );
}

interface KeyboardHintProps {
  visible: boolean;
}

function KeyboardHint({ visible }: KeyboardHintProps) {
  if (!visible) return null;
  return (
    <div className="keyboard-hint">
      <span>↑↓ Scroll</span>
      <span>←→ Prev/Next</span>
      <span>Space Next</span>
      <span>+/- Zoom</span>
      <span>0 Reset</span>
      <span>F Fit</span>
      <span>M Mag</span>
      <span>[ ] Chapter</span>
    </div>
  );
}

// ─── Main Reader Component ───────────────────────────────────────────────────

interface ReaderProps {
  params: Promise<{ slug: string; chapter: string }>;
}

export default function EnhancedReader(props: ReaderProps) {
  const params = use(props.params);
  const router = useRouter();
  const { slug: seriesSlug, chapter: chapterNumber } = params;
  
  const [chapter, setChapter] = useState<Chapter | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  const [currentPage, setCurrentPage] = useState(1);
  const [readingMode, setReadingMode] = useState<ReadingMode>("vertical");
  const [spreadMode, setSpreadMode] = useState<SpreadMode>("single");
  const [zoom, setZoom] = useState(100);
  const [fitWidth, setFitWidth] = useState(false);
  const [showControls, setShowControls] = useState(true);
  const [showHint, setShowHint] = useState(false);
  const [magnifier, setMagnifier] = useState(false);
  
  // Infinity scroll state - stores all loaded pages
  const [allPages, setAllPages] = useState<Page[]>([]);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [currentChapterNum, setCurrentChapterNum] = useState<number>(0);
  const [lastChapterNum, setLastChapterNum] = useState<number>(0);
  
  const viewportRef = useRef<HTMLDivElement>(null);
  const progressTimerRef = useRef<NodeJS.Timeout | null>(null);

  // Load chapter data
  useEffect(() => {
    if (!seriesSlug || !chapterNumber) return;
    
    const loadChapter = async () => {
      setLoading(true);
      setError(null);
      
      try {
        const res = await fetch(`${API}/library/${seriesSlug}/chapter/${chapterNumber}`);
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        
        const data = await res.json();
        
        // Load saved progress
        const saved = loadProgress(seriesSlug, data.chapter_uuid);
        
        const chapterData = {
          uuid: data.chapter_uuid,
          name: data.chapter_name,
          index: 0,
          pages: data.pages,
          prev_chapter_uuid: data.prev_chapter_uuid,
          next_chapter_uuid: data.next_chapter_uuid,
          chapter_number: parseFloat(chapterNumber),
        };
        
        setChapter(chapterData);
        
        // For infinity scroll mode, store all pages
        const chapterNum = parseFloat(chapterNumber);
        setCurrentChapterNum(chapterNum);
        setLastChapterNum(data.next_chapter_uuid ? chapterNum + 1 : chapterNum);
        
        // Initialize all pages for infinity scroll
        if (readingMode === "scroll") {
          setAllPages(data.pages);
        }
        
        // Restore saved state
        if (saved) {
          setCurrentPage(Math.min(saved.page, data.pages.length));
          if (saved.readingMode) setReadingMode(saved.readingMode);
          if (saved.zoom) setZoom(saved.zoom);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load chapter");
      } finally {
        setLoading(false);
      }
    };
    
    loadChapter();
  }, [seriesSlug, chapterNumber]);

  // Load next chapter for infinity scroll
  const loadNextChapter = useCallback(async () => {
    if (isLoadingMore || !chapter?.next_chapter_uuid) return;
    
    const nextNum = currentChapterNum + 1;
    setIsLoadingMore(true);
    
    try {
      const res = await fetch(`${API}/library/${seriesSlug}/chapter/${nextNum}`);
      if (!res.ok) return; // No more chapters
      
      const data = await res.json();
      
      // Append pages to allPages
      setAllPages(prev => [...prev, ...data.pages]);
      setLastChapterNum(nextNum);
      setCurrentChapterNum(nextNum);
      
      // Update chapter with new data
      setChapter({
        uuid: data.chapter_uuid,
        name: data.chapter_name,
        index: 0,
        pages: data.pages,
        prev_chapter_uuid: data.prev_chapter_uuid,
        next_chapter_uuid: data.next_chapter_uuid,
        chapter_number: nextNum,
      });
    } catch (err) {
      console.error("Failed to load next chapter:", err);
    } finally {
      setIsLoadingMore(false);
    }
  }, [seriesSlug, chapter, currentChapterNum, isLoadingMore]);

  // Scroll detection for infinity scroll
  useEffect(() => {
    if (readingMode !== "scroll" || !viewportRef.current) return;
    
    const viewport = viewportRef.current;
    
    const handleScroll = () => {
      const scrollTop = viewport.scrollTop;
      const scrollHeight = viewport.scrollHeight;
      const clientHeight = viewport.clientHeight;
      
      // Load more when within 500px of bottom
      const threshold = 500;
      if (scrollHeight - scrollTop - clientHeight < threshold) {
        loadNextChapter();
      }
    };
    
    viewport.addEventListener("scroll", handleScroll);
    return () => viewport.removeEventListener("scroll", handleScroll);
  }, [readingMode, loadNextChapter]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if in input
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) {
        return;
      }
      
      if (!chapter) return;
      
      switch (e.key) {
        case "ArrowUp":
        case "ArrowDown":
          // Vertical scroll handled by browser
          break;
        case "ArrowLeft":
          if (readingMode === "horizontal") {
            setCurrentPage(p => Math.max(1, p - (spreadMode === "spread" ? 2 : 1)));
          }
          break;
        case "ArrowRight":
          if (readingMode === "horizontal") {
            setCurrentPage(p => Math.min(chapter.pages.length, p + (spreadMode === "spread" ? 2 : 1)));
          }
          break;
        case " ":
          e.preventDefault();
          if (readingMode === "vertical") {
            setCurrentPage(p => Math.min(chapter.pages.length, p + 1));
          }
          break;
        case "+":
        case "=":
          setZoom(z => Math.min(ZOOM_MAX, z + ZOOM_STEP));
          break;
        case "-":
          setZoom(z => Math.max(ZOOM_MIN, z - ZOOM_STEP));
          break;
        case "0":
          setZoom(100);
          setFitWidth(false);
          break;
        case "f":
        case "F":
          setFitWidth(f => !f);
          break;
        case "m":
        case "M":
          setMagnifier(m => !m);
          break;
        case "[":
          if (chapter.prev_chapter_uuid) {
            window.location.href = `/reader/${seriesSlug}/${parseFloat(chapterNumber) - 1}`;
          }
          break;
        case "]":
          if (chapter.next_chapter_uuid) {
            window.location.href = `/reader/${seriesSlug}/${parseFloat(chapterNumber) + 1}`;
          }
          break;
        case "Home":
          setCurrentPage(1);
          break;
        case "End":
          setCurrentPage(chapter.pages.length);
          break;
        case "?":
          setShowHint(h => !h);
          break;
      }
    };
    
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [chapter, readingMode, spreadMode, seriesSlug, chapterNumber]);

  // Auto-save progress
  useEffect(() => {
    if (!chapter) return;
    
    if (progressTimerRef.current) {
      clearInterval(progressTimerRef.current);
    }
    
    progressTimerRef.current = setInterval(() => {
      saveProgress(seriesSlug, chapter.uuid, currentPage, readingMode, zoom);
    }, PROGRESS_SAVE_INTERVAL);
    
    return () => {
      if (progressTimerRef.current) {
        clearInterval(progressTimerRef.current);
      }
    };
  }, [chapter, currentPage, readingMode, zoom, seriesSlug]);

  // Scroll to current page in vertical mode
  useEffect(() => {
    if (readingMode !== "vertical" || !viewportRef.current) return;
    
    const viewport = viewportRef.current;
    const pageElements = viewport.querySelectorAll(".page");
    const pageElement = pageElements[currentPage - 1];
    
    if (pageElement) {
      pageElement.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [currentPage, readingMode]);

  // Navigation handlers
  const goToPage = useCallback((page: number) => {
    setCurrentPage(Math.max(1, Math.min(page, chapter?.pages.length || 1)));
  }, [chapter]);

  const handlePrevChapter = useCallback(() => {
    if (chapter?.prev_chapter_uuid) {
      router.push(`/reader/${seriesSlug}/${parseFloat(chapterNumber) - 1}`);
    }
  }, [chapter, router, seriesSlug, chapterNumber]);

  const handleNextChapter = useCallback(() => {
    if (chapter?.next_chapter_uuid) {
      router.push(`/reader/${seriesSlug}/${parseFloat(chapterNumber) + 1}`);
    }
  }, [chapter, router, seriesSlug, chapterNumber]);

  // Loading state
  if (loading) {
    return (
      <div className="reader-loading">
        <div className="spinner" />
        <p>Loading chapter...</p>
      </div>
    );
  }

  // Error state
  if (error || !chapter) {
    return (
      <div className="reader-error">
        <p>⚠️ {error || "Chapter not found"}</p>
        <button className="btn" onClick={() => router.push(`/series/${seriesSlug}`)}>
          Go to Series
        </button>
      </div>
    );
  }

  return (
    <div className={`reader ${readingMode}`}>
      <TopBar
        title={chapter.name}
        currentPage={currentPage}
        totalPages={chapter.pages.length}
        readingMode={readingMode}
        spreadMode={spreadMode}
        zoom={zoom}
        hasPrev={!!chapter.prev_chapter_uuid}
        hasNext={!!chapter.next_chapter_uuid}
        onBack={() => router.push(`/series/${seriesSlug}`)}
        onModeChange={setReadingMode}
        onSpreadChange={setSpreadMode}
        onZoomChange={setZoom}
        onPrevChapter={handlePrevChapter}
        onNextChapter={handleNextChapter}
      />

      <div 
        ref={viewportRef}
        className="reader-viewport"
        onClick={(e) => {
          // Click to advance page in horizontal mode
          if (readingMode === "horizontal" && e.target === e.currentTarget) {
            const rect = e.currentTarget.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const isLeftHalf = clickX < rect.width / 2;
            
            if (isLeftHalf) {
              goToPage(currentPage - (spreadMode === "spread" ? 2 : 1));
            } else {
              goToPage(currentPage + (spreadMode === "spread" ? 2 : 1));
            }
          }
        }}
      >
        {readingMode === "vertical" ? (
          <div className="pages-vertical">
            {chapter.pages.map((page, idx) => (
              <PageImage
                key={page.page_number}
                page={page}
                zoom={zoom}
                fitWidth={fitWidth}
              />
            ))}
          </div>
        ) : readingMode === "scroll" ? (
          <div className="pages-vertical">
            {allPages.map((page, idx) => (
              <PageImage
                key={`${page.page_number}-${idx}`}
                page={page}
                zoom={zoom}
                fitWidth={true}
              />
            ))}
            {isLoadingMore && (
              <div style={{ padding: '20px', textAlign: 'center', color: 'var(--muted)' }}>
                Loading more...
              </div>
            )}
          </div>
        ) : spreadMode === "spread" ? (
          <div className="pages-spread">
            {chapter.pages.reduce((acc: React.ReactNode[], page, idx) => {
              if (idx % 2 === 0) {
                acc.push(
                  <Spread
                    key={page.page_number}
                    leftPage={page}
                    rightPage={chapter.pages[idx + 1] || null}
                    zoom={zoom}
                  />
                );
              }
              return acc;
            }, [] as React.ReactNode[])}
          </div>
        ) : (
          <div className="pages-horizontal">
            {chapter.pages.map((page) => (
              <PageImage
                key={page.page_number}
                page={page}
                zoom={zoom}
                fitWidth={fitWidth}
              />
            ))}
          </div>
        )}
      </div>

      <KeyboardHint visible={showHint} />

      {/* Floating Home Button */}
      <button
        className="fab-home"
        onClick={() => router.push("/")}
        title="Go Home"
      >
        ∞
      </button>
    </div>
  );
}
