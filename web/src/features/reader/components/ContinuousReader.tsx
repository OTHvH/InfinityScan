"use client";

import { memo, useCallback, useEffect, useRef } from "react";
import { Virtuoso, type ListRange } from "react-virtuoso";
import { ChapterSeparator } from "./ChapterSeparator";
import { ReaderErrorFooter } from "./ReaderErrorFooter";
import { ReaderLoadingFooter } from "./ReaderLoadingFooter";
import { ReaderPage } from "./ReaderPage";
import type { ReaderItem, ReaderRequestReason } from "../types";

interface ContinuousReaderProps {
  items: ReaderItem[];
  firstItemIndex: number;
  hasMoreNext: boolean;
  hasMorePrevious: boolean;
  loadingNext: boolean;
  loadingPrevious: boolean;
  error: string | null;
  zoom: number;
  fitWidth: boolean;
  onLoadNext: (reason: Extract<ReaderRequestReason, "endReached" | "footerObserver" | "modeTransition">) => Promise<unknown> | null;
  onLoadPrevious: (reason: Extract<ReaderRequestReason, "prepend" | "modeTransition">) => Promise<unknown> | null;
  onRetry: () => Promise<unknown> | null;
  onRangeChanged: (range: ListRange) => void;
  initialTopMostItemIndex?: number;
  registerPageCleanup?: (pageId: string, cleanup: () => void) => () => void;
}

function FooterSentinel({ onIntersect }: { onIntersect: () => void }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) onIntersect();
    }, { rootMargin: "1200px 0px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, [onIntersect]);
  return <div ref={ref} aria-hidden="true" style={{ height: 1 }} />;
}

function ContinuousReaderView(props: ContinuousReaderProps) {
  const {
    hasMoreNext,
    loadingNext,
    hasMorePrevious,
    loadingPrevious,
    onLoadNext,
    onLoadPrevious,
    onRetry,
  } = props;
  const nextRequestRef = useRef<Promise<unknown> | null>(null);
  const triggerNext = useCallback((reason: Extract<ReaderRequestReason, "endReached" | "footerObserver">) => {
    if (!hasMoreNext || loadingNext || nextRequestRef.current) return;
    const request = onLoadNext(reason);
    if (!request) return;
    nextRequestRef.current = request;
    void request.then(
      () => { if (nextRequestRef.current === request) nextRequestRef.current = null; },
      () => { if (nextRequestRef.current === request) nextRequestRef.current = null; },
    );
  }, [hasMoreNext, loadingNext, onLoadNext]);

  const triggerPrevious = useCallback(() => {
    if (!hasMorePrevious || loadingPrevious) return;
    void onLoadPrevious("prepend")?.catch(() => undefined);
  }, [hasMorePrevious, loadingPrevious, onLoadPrevious]);

  const triggerRetry = useCallback(() => {
    void onRetry()?.catch(() => undefined);
  }, [onRetry]);

  return (
    <Virtuoso
      data={props.items}
      firstItemIndex={props.firstItemIndex}
      initialTopMostItemIndex={props.initialTopMostItemIndex}
      increaseViewportBy={{ top: 700, bottom: 1200 }}
      computeItemKey={(_, item) => item.key}
      scrollSeekConfiguration={{
        enter: (velocity) => Math.abs(velocity) > 900,
        exit: (velocity) => Math.abs(velocity) < 300,
      }}
      components={{
        ScrollSeekPlaceholder: ({ index, height }) => (
          <div className="page page-placeholder" style={{ height }} aria-label={`Loading reader item ${index + 1}`} />
        ),
        Footer: () => (
          <>
            <ReaderLoadingFooter loading={props.loadingNext} />
            <ReaderErrorFooter error={props.error} onRetry={triggerRetry} />
            <FooterSentinel onIntersect={() => triggerNext("footerObserver")} />
          </>
        ),
      }}
      itemContent={(_, item) => item.kind === "chapter-separator"
        ? <ChapterSeparator item={item} />
        : <ReaderPage item={item} zoom={props.zoom} fitWidth={props.fitWidth} registerCleanup={props.registerPageCleanup} />}
      endReached={() => triggerNext("endReached")}
      startReached={triggerPrevious}
      rangeChanged={props.onRangeChanged}
      style={{ height: "100%", paddingTop: 56, paddingBottom: 60 }}
    />
  );
}

export const ContinuousReader = memo(ContinuousReaderView);
