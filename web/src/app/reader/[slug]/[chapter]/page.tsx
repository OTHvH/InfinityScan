"use client";

import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { Virtuoso } from "react-virtuoso";
import { useSearchParams } from "next/navigation";

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
}

interface ReaderState {
  seriesSlug: string;
  chapterUuid: string;
  readingMode: "vertical" | "horizontal";
  currentPage: number;
  scrollPosition: number;
}

// ─── Constants ────────────────────────────────────────────────────────────────

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const PRELOAD_THRESHOLD = 3; // Preload next chapter when within 3 pages of end
const PROGRESS_SAVE_INTERVAL = 3000; // Save progress every 3 seconds

// Keyboard shortcuts - define action and optional mode restriction
type ShortcutAction = "prev" | "next" | "first" | "last";
type ShortcutMode = "vertical" | "horizontal" | null;

interface Shortcut {
  action: ShortcutAction;
  mode?: ShortcutMode;
}

const SHORTCUTS: Record<string, Shortcut> = {
  ArrowUp: { action: "prev", mode: "vertical" },
  ArrowDown: { action: "next", mode: "vertical" },
  ArrowLeft: { action: "prev", mode: "horizontal" },
  ArrowRight: { action: "next", mode: "horizontal" },
  Space: { action: "next", mode: "vertical" },
  Home: { action: "first" },
  End: { action: "last" },
};

// ─── Helpers ────────────────────────────────────────────────────────────────

function getUserId(): string {
  const key = "infinityscan_user_id";
  let userId = localStorage.getItem(key);
  if (!userId) {
    userId = crypto.randomUUID();
    localStorage.setItem(key, userId);
  }
  return userId;
}

function getStorageKey(seriesSlug: string, chapterUuid: string): string {
  return `infinityscan_progress_${seriesSlug}_${chapterUuid}`;
}

async function syncProgressToBackend(
  seriesSlug: string,
  chapterUuid: string,
  page: number,
  completed: boolean = false
) {
  try {
    const userId = getUserId();
    await fetch(`${API}/progress/${seriesSlug}/${chapterUuid}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-User-ID": userId,
      },
      body: JSON.stringify({
        chapter_uuid: chapterUuid,
        last_page: page,
        completed,
      }),
    });
  } catch (e) {
    // Silently fail - localStorage is backup
    console.debug("Failed to sync progress to backend:", e);
  }
}

function saveProgress(seriesSlug: string, chapterUuid: string, page: number, completed: boolean = false) {
  try {
    const key = getStorageKey(seriesSlug, chapterUuid);
    localStorage.setItem(key, JSON.stringify({ page, updatedAt: Date.now() }));
    // Also sync to backend
    syncProgressToBackend(seriesSlug, chapterUuid, page, completed);
  } catch (e) {
    console.error("Failed to save progress:", e);
  }
}

function loadProgress(seriesSlug: string, chapterUuid: string): number | null {
  try {
    const key = getStorageKey(seriesSlug, chapterUuid);
    const saved = localStorage.getItem(key);
    if (saved) {
      const data = JSON.parse(saved);
      return data.page ?? null;
    }
  } catch (e) {
    console.error("Failed to load progress:", e);
  }
  return null;
}

async function loadProgressFromBackend(
  seriesSlug: string,
  chapterUuid: string
): Promise<number | null> {
  try {
    const userId = getUserId();
    const res = await fetch(
      `${API}/progress/${seriesSlug}/${chapterUuid}`,
      {
        headers: { "X-User-ID": userId },
      }
    );
    if (res.ok) {
      const data = await res.json();
      return data.last_page ?? null;
    }
  } catch (e) {
    console.debug("Failed to load progress from backend:", e);
  }
  return null;
}

// ─── Components ───────────────────────────────────────────────────────────────

interface ReaderControlsProps {
  chapter: Chapter;
  currentPage: number;
  totalPages: number;
  readingMode: "vertical" | "horizontal";
  onModeChange: (mode: "vertical" | "horizontal") => void;
  onChapterChange: (uuid: string) => void;
  onPageChange: (page: number) => void;
  onClose: () => void;
}

function ReaderControls({
  chapter,
  currentPage,
  totalPages,
  readingMode,
  onModeChange,
  onChapterChange,
  onPageChange,
  onClose,
}: ReaderControlsProps) {
  const progress = Math.round((currentPage / totalPages) * 100);

  return (
    <>
      {/* Top bar */}
      <div className="reader-top-bar">
        <button className="btn ghost small" onClick={onClose}>
          ← Back
        </button>
        <span className="chapter-title">{chapter.name}</span>
        <span className="page-indicator">
          {currentPage} / {totalPages}
        </span>
      </div>

      {/* Progress bar */}
      <div className="progress-wrap">
        <div className="progress-bar" style={{ width: `${progress}%` }} />
      </div>

      {/* Bottom controls */}
      <div className="reader-controls">
        <button
          className={`btn small ${readingMode === "vertical" ? "active" : "ghost"}`}
          onClick={() => onModeChange("vertical")}
        >
          ↓ Vertical
        </button>
        <button
          className={`btn small ${readingMode === "horizontal" ? "active" : "ghost"}`}
          onClick={() => onModeChange("horizontal")}
        >
          → Horizontal
        </button>

        <div className="page-nav">
          {chapter.prev_chapter_uuid && (
            <button
              className="btn ghost small"
              onClick={() => onChapterChange(chapter.prev_chapter_uuid!)}
            >
              ← Prev
            </button>
          )}
          <input
            type="number"
            min={1}
            max={totalPages}
            value={currentPage}
            onChange={(e) => onPageChange(parseInt(e.target.value) || 1)}
            className="page-input"
          />
          {chapter.next_chapter_uuid && (
            <button
              className="btn ghost small"
              onClick={() => onChapterChange(chapter.next_chapter_uuid!)}
            >
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
  index: number;
  isVisible: boolean;
  readingMode: "vertical" | "horizontal";
}

function PageImage({ page, index, isVisible, readingMode }: PageImageProps) {
  const [loaded, setLoaded] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);

  // Only render image if visible (lazy loading)
  if (!isVisible) {
    return (
      <div
        className="page-placeholder"
        style={{ aspectRatio: "auto", minHeight: readingMode === "vertical" ? "400px" : "100vh" }}
      />
    );
  }

  return (
    <div className={`page ${readingMode === "horizontal" ? "page-horizontal" : ""}`}>
      <img
        ref={imgRef}
        src={page.url}
        alt={`Page ${page.page_number}`}
        loading="lazy"
        onLoad={() => setLoaded(true)}
        className={`page-img ${loaded ? "loaded" : "loading"}`}
      />
    </div>
  );
}

interface ReaderProps {
  seriesSlug: string;
  chapterUuid: string;
}

export default function Reader({ seriesSlug, chapterUuid }: ReaderProps) {
  const [chapter, setChapter] = useState<Chapter | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [readingMode, setReadingMode] = useState<"vertical" | "horizontal">("vertical");
  const [preloadedChapter, setPreloadedChapter] = useState<Chapter | null>(null);
  
  const virtuosoRef = useRef<any>(null);
  const progressTimerRef = useRef<NodeJS.Timeout | null>(null);
  const searchParams = useSearchParams();

  // Load chapter data
  useEffect(() => {
    const loadChapter = async () => {
      setLoading(true);
      setError(null);
      
      try {
        const res = await fetch(`${API}/series/${seriesSlug}/chapter/${chapterUuid}`);
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        
        const data = await res.json();
        
        // Transform API response to our format
        const transformed: Chapter = {
          uuid: data.chapter_uuid,
          name: data.chapter_name,
          index: 0,
          pages: data.pages,
          prev_chapter_uuid: data.prev_chapter_uuid,
          next_chapter_uuid: data.next_chapter_uuid,
        };
        
        setChapter(transformed);
        
        // Load saved progress from localStorage
        const savedPage = loadProgress(seriesSlug, chapterUuid);
        if (savedPage && savedPage > 0 && savedPage <= transformed.pages.length) {
          setCurrentPage(savedPage);
        } else {
          // Try loading from backend
          const backendPage = await loadProgressFromBackend(seriesSlug, chapterUuid);
          if (backendPage && backendPage > 0 && backendPage <= transformed.pages.length) {
            setCurrentPage(backendPage);
          }
        }
        
        // Start progress save timer
        startProgressTimer();
        
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load chapter");
      } finally {
        setLoading(false);
      }
    };
    
    loadChapter();
    
    return () => {
      if (progressTimerRef.current) {
        clearInterval(progressTimerRef.current);
      }
    };
  }, [seriesSlug, chapterUuid]);

  // Preload next chapter
  useEffect(() => {
    if (!chapter?.next_chapter_uuid) return;
    
    const pageIndex = currentPage - 1;
    const distanceToEnd = chapter.pages.length - pageIndex;
    
    if (distanceToEnd <= PRELOAD_THRESHOLD) {
      const preloadNext = async () => {
        try {
          const res = await fetch(
            `${API}/series/${seriesSlug}/chapter/${chapter.next_chapter_uuid}`
          );
          if (res.ok) {
            const data = await res.json();
            setPreloadedChapter({
              uuid: data.chapter_uuid,
              name: data.chapter_name,
              index: 0,
              pages: data.pages,
              prev_chapter_uuid: data.prev_chapter_uuid,
              next_chapter_uuid: data.next_chapter_uuid,
            });
          }
        } catch (e) {
          console.error("Failed to preload next chapter:", e);
        }
      };
      
      preloadNext();
    }
  }, [chapter, currentPage, seriesSlug]);

  // Progress save timer
  const startProgressTimer = useCallback(() => {
    if (progressTimerRef.current) {
      clearInterval(progressTimerRef.current);
    }
    
    progressTimerRef.current = setInterval(() => {
      saveProgress(seriesSlug, chapterUuid, currentPage);
    }, PROGRESS_SAVE_INTERVAL);
  }, [seriesSlug, chapterUuid, currentPage]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if in input
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) {
        return;
      }
      
      const shortcut = SHORTCUTS[e.key as keyof typeof SHORTCUTS];
      if (!shortcut) return;
      
      // Check mode-specific shortcuts
      if (shortcut.mode && readingMode !== shortcut.mode) return;
      
      e.preventDefault();
      
      if (!chapter) return;
      
      switch (shortcut.action) {
        case "prev":
          if (currentPage > 1) {
            goToPage(currentPage - 1);
          } else if (chapter.prev_chapter_uuid) {
            // Navigate to previous chapter
          }
          break;
        case "next":
          if (currentPage < chapter.pages.length) {
            goToPage(currentPage + 1);
          } else if (chapter.next_chapter_uuid && preloadedChapter) {
            // Navigate to next chapter (preloaded)
          }
          break;
        case "first":
          goToPage(1);
          break;
        case "last":
          goToPage(chapter.pages.length);
          break;
      }
    };
    
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [chapter, currentPage, readingMode, preloadedChapter]);

  // Navigate to page
  const goToPage = useCallback((page: number) => {
    if (!chapter) return;
    
    const validPage = Math.max(1, Math.min(page, chapter.pages.length));
    setCurrentPage(validPage);
    
    // Scroll to page using Virtuoso
    if (virtuosoRef.current) {
      virtuosoRef.current.scrollToIndex({
        index: validPage - 1,
        behavior: "smooth",
        align: readingMode === "vertical" ? "start" : "center",
      });
    }
  }, [chapter, readingMode]);

  // Handle scroll position tracking
  const handleScroll = useCallback((event: React.UIEvent<HTMLDivElement>) => {
    if (!chapter) return;
    
    const target = event.currentTarget;
    const scrollTop = target.scrollTop;
    const viewportHeight = target.clientHeight;
    
    const estimatedPage = Math.floor(scrollTop / viewportHeight) + 1;
    if (estimatedPage !== currentPage && estimatedPage > 0 && estimatedPage <= chapter.pages.length) {
      setCurrentPage(estimatedPage);
    }
  }, [chapter, currentPage]);

  // Change reading mode
  const handleModeChange = useCallback((mode: "vertical" | "horizontal") => {
    setReadingMode(mode);
  }, []);

  // Change chapter
  const handleChapterChange = useCallback((uuid: string) => {
    // Save current progress before switching
    saveProgress(seriesSlug, chapterUuid, currentPage);
    // Navigate to new chapter (via URL)
    window.location.href = `/reader/${seriesSlug}/${uuid}`;
  }, [seriesSlug, chapterUuid, currentPage]);

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
  if (error) {
    return (
      <div className="reader-error">
        <p>⚠️ {error}</p>
        <button className="btn" onClick={() => window.history.back()}>
          Go Back
        </button>
      </div>
    );
  }

  // No chapter data
  if (!chapter) {
    return (
      <div className="reader-error">
        <p>Chapter not found</p>
        <button className="btn" onClick={() => window.history.back()}>
          Go Back
        </button>
      </div>
    );
  }

  return (
    <div className={`reader ${readingMode}`}>
      <ReaderControls
        chapter={chapter}
        currentPage={currentPage}
        totalPages={chapter.pages.length}
        readingMode={readingMode}
        onModeChange={handleModeChange}
        onChapterChange={handleChapterChange}
        onPageChange={goToPage}
        onClose={() => window.history.back()}
      />
      
      <div className="reader-viewport">
        {readingMode === "vertical" ? (
          <Virtuoso
            ref={virtuosoRef}
            totalCount={chapter.pages.length}
            itemContent={(index) => (
              <PageImage
                page={chapter.pages[index]}
                index={index}
                isVisible={true}
                readingMode={readingMode}
              />
            )}
            overscan={200}
            style={{ height: "100%" }}
          />
        ) : (
          <div className="horizontal-container" style={{ display: 'flex', flexDirection: 'row', overflowX: 'auto', height: '100%' }}>
            <Virtuoso
              ref={virtuosoRef}
              totalCount={chapter.pages.length}
              itemContent={(index) => (
                <PageImage
                  page={chapter.pages[index]}
                  index={index}
                  isVisible={true}
                  readingMode={readingMode}
                />
              )}
              style={{ height: "100%", width: "max-content" }}
            />
          </div>
        )}
      </div>
      
      {/* Next chapter banner if preloaded */}
      {preloadedChapter && currentPage >= chapter.pages.length - PRELOAD_THRESHOLD && (
        <div className="preload-banner">
          <p>Next chapter ready: {preloadedChapter.name}</p>
          <button
            className="btn small"
            onClick={() => handleChapterChange(preloadedChapter.uuid)}
          >
            Read Now →
          </button>
        </div>
      )}
      
      {/* Keyboard shortcuts hint */}
      <div className="shortcuts-hint">
        <span>↑↓ Scroll</span>
        <span>←→ Prev/Next</span>
        <span>Home/End First/Last</span>
      </div>
    </div>
  );
}
