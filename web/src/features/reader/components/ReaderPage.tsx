"use client";

import { memo, useEffect, useRef, useState } from "react";
import { resolvePageMediaRequest } from "@/lib/api";
import {
  cachedPageImage,
  cachePageImage,
  evictCachedPageImage,
} from "../media-cache";
import type { ReaderItem } from "../types";

type PageItem = Extract<ReaderItem, { kind: "page" }>;
export type PageImageState = "waiting" | "loading" | "loaded" | "retrying" | "unavailable";
type PageImageError = "temporary" | "not-found" | "unavailable" | "decode" | null;

interface ReaderPageProps {
  item: PageItem;
  zoom: number;
  fitWidth: boolean;
  seeking?: boolean;
  registerCleanup?: (pageId: string, cleanup: () => void) => () => void;
}

interface ImageViewState {
  status: PageImageState;
  error: PageImageError;
  source: string | null;
  attempt: number;
  generation: number;
}

const MAX_RETRIES = 3;
const MAX_ATTEMPTS = MAX_RETRIES + 1;
const RETRY_BASE_DELAY_MS = 500;
const RETRY_MAX_DELAY_MS = 2000;

function initialState(): ImageViewState {
  return { status: "waiting", error: null, source: null, attempt: 0, generation: 0 };
}

function ReaderPageView({ item, zoom, fitWidth, seeking = false, registerCleanup }: ReaderPageProps) {
  const [view, setView] = useState<ImageViewState>(initialState);
  const retryNowRef = useRef<(() => void) | null>(null);
  const generationRef = useRef(0);
  const objectUrlRef = useRef<string | null>(null);
  const aspectRatio = item.width > 0 && item.height > 0
    ? `${item.width} / ${item.height}`
    : `${item.aspectRatio || 2 / 3}`;

  useEffect(() => {
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;
    let attempt = 0;
    const generation = generationRef.current + 1;
    generationRef.current = generation;

    const clearRetryTimer = () => {
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = null;
    };
    const releaseObjectUrl = () => {
      objectUrlRef.current = null;
    };
    const stop = () => {
      active = false;
      generationRef.current += 1;
      clearRetryTimer();
      controller?.abort();
      controller = null;
      releaseObjectUrl();
      retryNowRef.current = null;
    };

    const startRequest = async (nextAttempt: number) => {
      if (!active || nextAttempt > MAX_ATTEMPTS) return;
      clearRetryTimer();
      controller?.abort();
      const cachedSource = nextAttempt === 1 ? cachedPageImage(item.pageId) : null;
      if (cachedSource) {
        attempt = 1;
        objectUrlRef.current = cachedSource;
        setView({ status: "loading", error: null, source: cachedSource, attempt, generation });
        return;
      }
      const requestController = new AbortController();
      controller = requestController;
      releaseObjectUrl();
      attempt = nextAttempt;
      setView({
        status: attempt === 1 ? "loading" : "retrying",
        error: attempt === 1 ? null : "temporary",
        source: null,
        attempt,
        generation,
      });

      let requestUrl: string;
      try {
        requestUrl = resolvePageMediaRequest(item.mediaPath, attempt);
      } catch {
        setView({ status: "unavailable", error: "unavailable", source: null, attempt, generation });
        return;
      }

      const scheduleRetry = () => {
        if (!active) return;
        if (attempt >= MAX_ATTEMPTS) {
          setView({ status: "unavailable", error: "temporary", source: null, attempt, generation });
          return;
        }
        setView({ status: "retrying", error: "temporary", source: null, attempt, generation });
        const delay = Math.min(RETRY_BASE_DELAY_MS * (2 ** (attempt - 1)), RETRY_MAX_DELAY_MS);
        retryTimer = setTimeout(() => { void startRequest(attempt + 1); }, delay);
      };

      try {
        const response = await fetch(requestUrl, {
          credentials: "omit",
          signal: requestController.signal,
        });
        if (!active || generationRef.current !== generation || controller !== requestController) return;
        if (response.status === 404) {
          setView({ status: "unavailable", error: "not-found", source: null, attempt, generation });
          return;
        }
        if (!response.ok) {
          scheduleRetry();
          return;
        }
        const blob = await response.blob();
        if (!active || generationRef.current !== generation || controller !== requestController) return;
        const source = cachePageImage(item.pageId, URL.createObjectURL(blob));
        objectUrlRef.current = source;
        setView({
          status: attempt === 1 ? "loading" : "retrying",
          error: null,
          source,
          attempt,
          generation,
        });
      } catch {
        if (!active || generationRef.current !== generation || controller !== requestController) return;
        if (requestController.signal.aborted) return;
        scheduleRetry();
      }
    };

    retryNowRef.current = () => {
      if (!active || attempt >= MAX_ATTEMPTS) return;
      void startRequest(attempt + 1);
    };
    const unregister = registerCleanup?.(item.pageId, () => {
      evictCachedPageImage(item.pageId);
      stop();
    });
    if (seeking) {
      queueMicrotask(() => { if (active) setView(initialState()); });
    } else {
      void startRequest(1);
    }
    return () => {
      stop();
      unregister?.();
    };
  }, [item.mediaPath, item.pageId, registerCleanup, seeking]);

  const handleImageLoad = async (image: HTMLImageElement) => {
    const { generation, source } = view;
    try {
      await image.decode?.();
      if (generationRef.current !== generation || objectUrlRef.current !== source) return;
      setView((current) => current.generation === generation
        ? { ...current, status: "loaded", error: null }
        : current);
    } catch {
      if (generationRef.current !== generation || objectUrlRef.current !== source) return;
      if (source) evictCachedPageImage(item.pageId, source);
      objectUrlRef.current = null;
      setView((current) => current.generation === generation
        ? { ...current, status: "unavailable", error: "decode", source: null }
        : current);
    }
  };

  const errorMessage = view.error === "not-found"
    ? "Page unavailable or not verified"
    : view.error === "decode"
      ? "Page image could not be decoded"
      : view.error === "temporary"
        ? "Temporary image loading failure"
        : "Page unavailable";
  const canRetry = view.attempt > 0 && view.attempt < MAX_ATTEMPTS
    && (view.error === "temporary" || view.error === "decode");

  return (
    <div
      className="page reader-page-frame"
      data-image-state={view.status}
      data-page-id={item.pageId}
      data-chapter-id={item.chapterId}
      style={{ aspectRatio, width: fitWidth ? "100%" : `${zoom}%` }}
    >
      {view.status !== "loaded" && <div className="reader-page-skeleton" aria-hidden="true" />}
      <img
        className={`reader-page-image ${view.status === "loaded" ? "loaded" : "pending"}`}
        src={view.source ?? undefined}
        alt={`Chapter ${item.chapterNumber}, page ${item.pageNumber}`}
        width={item.width || undefined}
        height={item.height || undefined}
        loading="lazy"
        decoding="async"
        onLoad={(event) => { void handleImageLoad(event.currentTarget); }}
      />
      {(view.status === "retrying" || view.status === "unavailable") && (
        <div className="reader-page-message" role="status">
          <span>{errorMessage}</span>
          {canRetry && (
            <button type="button" className="btn ghost small" onClick={() => retryNowRef.current?.()}>
              Retry
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export const ReaderPage = memo(ReaderPageView);
