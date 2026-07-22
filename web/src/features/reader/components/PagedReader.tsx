"use client";

import { memo } from "react";
import { Virtuoso, type ListRange } from "react-virtuoso";
import type { ReaderChapter, ReaderDirection, ReaderItem, ReaderPage as ReaderPageData, ReadingDirection, ReadingMode, SpreadMode } from "../types";
import { ReaderPage } from "./ReaderPage";

type PagedMode = Exclude<ReadingMode, "continuous">;
type PageItem = Extract<ReaderItem, { kind: "page" }>;

export interface PagedReaderProps {
  chapter: ReaderChapter;
  currentPageId: string | null;
  mode: PagedMode;
  spreadMode: SpreadMode;
  direction: ReadingDirection;
  firstPageAlone: boolean;
  zoom: number;
  fitWidth: boolean;
  onVisiblePageChange: (page: ReaderPageData) => void;
}

function toPageItem(chapter: ReaderChapter, page: ReaderPageData): PageItem {
  return {
    kind: "page",
    key: `page:${page.id}`,
    chapterId: chapter.id,
    chapterNumber: chapter.number,
    pageId: page.id,
    pageNumber: page.pageNumber,
    mediaPath: page.mediaPath,
    width: page.width,
    height: page.height,
    aspectRatio: page.aspectRatio,
  };
}

export function getSpreadIndices(
  currentIndex: number,
  pageCount: number,
  firstPageAlone: boolean,
  direction: ReadingDirection,
): number[] {
  if (pageCount <= 0) return [];
  const bounded = Math.min(Math.max(0, currentIndex), pageCount - 1);
  let indices: number[];
  if (firstPageAlone && bounded === 0) {
    indices = [0];
  } else {
    const offset = firstPageAlone ? 1 : 0;
    const start = offset + Math.floor((Math.max(offset, bounded) - offset) / 2) * 2;
    indices = [start, start + 1].filter((index) => index < pageCount);
  }
  return direction === "rtl" ? indices.reverse() : indices;
}

export function movePagedIndex(
  currentIndex: number,
  delta: -1 | 1,
  pageCount: number,
  spreadMode: SpreadMode,
  firstPageAlone: boolean,
): number {
  if (pageCount <= 0) return 0;
  if (spreadMode === "single") return Math.min(pageCount - 1, Math.max(0, currentIndex + delta));
  const pairs: number[][] = [];
  let index = 0;
  if (firstPageAlone) {
    pairs.push([0]);
    index = 1;
  }
  while (index < pageCount) {
    pairs.push([index, ...(index + 1 < pageCount ? [index + 1] : [])]);
    index += 2;
  }
  const pairIndex = Math.max(0, pairs.findIndex((pair) => pair.includes(currentIndex)));
  const targetPair = pairs[Math.min(pairs.length - 1, Math.max(0, pairIndex + delta))];
  return targetPair?.[0] ?? 0;
}

export function chapterDirectionForPageAction(direction: ReadingDirection, visualDirection: "left" | "right"): ReaderDirection {
  const forward = direction === "ltr" ? visualDirection === "right" : visualDirection === "left";
  return forward ? "next" : "previous";
}

function PagedReaderView(props: PagedReaderProps) {
  const currentIndex = Math.max(0, props.chapter.pages.findIndex((page) => page.id === props.currentPageId));
  if (props.mode === "vertical") {
    const items = props.chapter.pages.map((page) => toPageItem(props.chapter, page));
    const onRangeChanged = (range: ListRange) => {
      const center = Math.round((range.startIndex + range.endIndex) / 2);
      const page = props.chapter.pages[Math.min(props.chapter.pages.length - 1, Math.max(0, center))];
      if (page) props.onVisiblePageChange(page);
    };
    return (
      <Virtuoso
        data={items}
        initialTopMostItemIndex={currentIndex}
        increaseViewportBy={{ top: 500, bottom: 800 }}
        computeItemKey={(_, page) => page.key}
        itemContent={(_, page) => <ReaderPage item={page} zoom={props.zoom} fitWidth={props.fitWidth} />}
        rangeChanged={onRangeChanged}
        style={{ height: "100%", paddingTop: 56, paddingBottom: 60 }}
      />
    );
  }

  const indices = props.spreadMode === "spread"
    ? getSpreadIndices(currentIndex, props.chapter.pages.length, props.firstPageAlone, props.direction)
    : [currentIndex];
  return (
    <div className={`paged-horizontal ${props.spreadMode}`} data-reading-direction={props.direction}>
      {indices.map((index) => {
        const page = props.chapter.pages[index];
        return page ? <ReaderPage key={page.id} item={toPageItem(props.chapter, page)} zoom={props.zoom} fitWidth={props.fitWidth} /> : null;
      })}
    </div>
  );
}

export const PagedReader = memo(PagedReaderView);
