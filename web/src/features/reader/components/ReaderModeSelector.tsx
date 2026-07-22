"use client";

import type { ReadingMode, SpreadMode } from "../types";

interface ReaderModeSelectorProps {
  mode: ReadingMode;
  spreadMode: SpreadMode;
  onModeChange: (mode: ReadingMode) => void;
  onSpreadModeChange: (mode: SpreadMode) => void;
}

const MODES: Array<{ value: ReadingMode; label: string }> = [
  { value: "continuous", label: "Continuous" },
  { value: "vertical", label: "Vertical chapter" },
  { value: "horizontal", label: "Horizontal" },
];

export function ReaderModeSelector(props: ReaderModeSelectorProps) {
  return (
    <div className="reader-mode-selector" role="group" aria-label="Reading mode">
      {MODES.map(({ value, label }) => (
        <button
          type="button"
          key={value}
          className={`btn tiny ${props.mode === value ? "active" : "ghost"}`}
          aria-pressed={props.mode === value}
          onClick={() => props.onModeChange(value)}
        >
          {label}
        </button>
      ))}
      {props.mode === "horizontal" && (
        <div role="group" aria-label="Horizontal page layout">
          <button type="button" className={`btn tiny ${props.spreadMode === "single" ? "active" : "ghost"}`} aria-pressed={props.spreadMode === "single"} onClick={() => props.onSpreadModeChange("single")}>Single</button>
          <button type="button" className={`btn tiny ${props.spreadMode === "spread" ? "active" : "ghost"}`} aria-pressed={props.spreadMode === "spread"} onClick={() => props.onSpreadModeChange("spread")}>Spread</button>
        </div>
      )}
    </div>
  );
}
