import type { ReadingDirection, ReadingMode, SpreadMode } from "./types";

export interface ReaderPreferences {
  mode: ReadingMode;
  spreadMode: SpreadMode;
  zoom: number;
  fitWidth: boolean;
  direction: ReadingDirection;
  firstPageAlone: boolean;
}

export const DEFAULT_READER_PREFERENCES: ReaderPreferences = {
  mode: "vertical",
  spreadMode: "single",
  zoom: 100,
  fitWidth: false,
  direction: "ltr",
  firstPageAlone: false,
};

function preferenceKey(seriesSlug: string): string {
  return `infinityscan_reader_preferences_${seriesSlug}`;
}

export function loadReaderPreferences(seriesSlug: string): ReaderPreferences | null {
  try {
    const value = JSON.parse(localStorage.getItem(preferenceKey(seriesSlug)) ?? "null") as Record<string, unknown> | null;
    if (!value) return null;
    const legacyMode = value.mode === "scroll" ? "continuous" : value.mode;
    return {
      mode: legacyMode === "continuous" || legacyMode === "vertical" || legacyMode === "horizontal"
        ? legacyMode
        : DEFAULT_READER_PREFERENCES.mode,
      spreadMode: value.spreadMode === "spread" ? "spread" : "single",
      zoom: typeof value.zoom === "number" && Number.isFinite(value.zoom)
        ? Math.min(160, Math.max(40, value.zoom))
        : DEFAULT_READER_PREFERENCES.zoom,
      fitWidth: typeof value.fitWidth === "boolean" ? value.fitWidth : DEFAULT_READER_PREFERENCES.fitWidth,
      direction: value.direction === "rtl" ? "rtl" : "ltr",
      firstPageAlone: typeof value.firstPageAlone === "boolean" ? value.firstPageAlone : false,
    };
  } catch {
    return null;
  }
}

export function saveReaderPreferences(seriesSlug: string, preferences: ReaderPreferences): void {
  try {
    localStorage.setItem(preferenceKey(seriesSlug), JSON.stringify(preferences));
  } catch {
    // Preferences are optional and never replace reading progress.
  }
}
