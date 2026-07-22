"use client";

import type { ProgressStatus } from "../progress";
import type { ReadingDirection, ReadingMode, SpreadMode } from "../types";
import { ReaderModeSelector } from "./ReaderModeSelector";

export const READER_ZOOM_MIN = 40;
export const READER_ZOOM_MAX = 160;
export const READER_ZOOM_STEP = 10;

export function clampReaderZoom(zoom: number): number {
  return Math.min(READER_ZOOM_MAX, Math.max(READER_ZOOM_MIN, zoom));
}

interface ReaderToolbarProps {
  title: string;
  page: number;
  totalPages: number;
  mode: ReadingMode;
  spreadMode: SpreadMode;
  zoom: number;
  fitWidth: boolean;
  direction: ReadingDirection;
  progressStatus: ProgressStatus;
  hasPreviousChapter: boolean;
  hasNextChapter: boolean;
  onBack: () => void;
  onModeChange: (mode: ReadingMode) => void;
  onSpreadModeChange: (mode: SpreadMode) => void;
  onZoomChange: (zoom: number) => void;
  onFitWidthChange: (fit: boolean) => void;
  onDirectionChange: (direction: ReadingDirection) => void;
  onPreviousChapter: () => void;
  onNextChapter: () => void;
  onPageChange: (page: number) => void;
  onPreviousPage: () => void;
  onNextPage: () => void;
}

export function ReaderToolbar(props: ReaderToolbarProps) {
  return (
    <>
      <div className="reader-top-bar" aria-describedby="reader-shortcuts">
        <button type="button" className="btn ghost small" onClick={props.onBack}>Back</button>
        <span className="chapter-title">{props.title}</span>
        <span className="page-indicator">{props.page} / {props.totalPages}</span>
        <ReaderModeSelector mode={props.mode} spreadMode={props.spreadMode} onModeChange={props.onModeChange} onSpreadModeChange={props.onSpreadModeChange} />
        <div className="control-group zoom-controls" aria-label="Page zoom">
          <button type="button" className="btn ghost tiny" aria-label="Zoom out" onClick={() => props.onZoomChange(clampReaderZoom(props.zoom - READER_ZOOM_STEP))}>-</button>
          <span className="zoom-value">{props.zoom}%</span>
          <button type="button" className="btn ghost tiny" aria-label="Zoom in" onClick={() => props.onZoomChange(clampReaderZoom(props.zoom + READER_ZOOM_STEP))}>+</button>
          <button type="button" className={`btn tiny ${props.fitWidth ? "active" : "ghost"}`} aria-pressed={props.fitWidth} onClick={() => props.onFitWidthChange(!props.fitWidth)}>Fit width</button>
          <button type="button" className="btn ghost tiny" aria-label="Reading direction" onClick={() => props.onDirectionChange(props.direction === "ltr" ? "rtl" : "ltr")}>{props.direction.toUpperCase()}</button>
        </div>
        <div className="control-group" aria-label="Chapter navigation">
          <button type="button" className="btn ghost tiny" disabled={!props.hasPreviousChapter} aria-keyshortcuts="[" onClick={props.onPreviousChapter}>Previous chapter</button>
          <button type="button" className="btn ghost tiny" disabled={!props.hasNextChapter} aria-keyshortcuts="]" onClick={props.onNextChapter}>Next chapter</button>
        </div>
      </div>
      <div className="reader-controls">
        <span>{props.title}</span>
        <span className="reader-save-status" role="status">{props.progressStatus}</span>
        {props.mode !== "continuous" && (
          <>
            <input className="page-input" aria-label="Current page" type="number" min={1} max={props.totalPages} value={props.page} onChange={(event) => props.onPageChange(Number.parseInt(event.target.value, 10) || 1)} />
            <button type="button" className="btn ghost small" onClick={props.onPreviousPage}>Previous page</button>
            <button type="button" className="btn ghost small" onClick={props.onNextPage}>Next page</button>
          </>
        )}
      </div>
      <div id="reader-shortcuts" className="sr-only">
        Left and right arrows change pages in paged modes. Space advances a page or spread. Left bracket opens the previous chapter and right bracket opens the next chapter. Shortcuts do not run while editing a form field.
      </div>
    </>
  );
}
