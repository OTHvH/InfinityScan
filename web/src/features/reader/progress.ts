import { api } from "@/lib/api";
import type { ReadingMode } from "./types";

export interface ReaderProgress {
  page: number;
  readingMode?: ReadingMode;
  zoom?: number;
}

function progressKey(seriesSlug: string, chapterId: string): string {
  return `infinityscan_progress_${seriesSlug}_${chapterId}`;
}

export function loadLocalProgress(seriesSlug: string, chapterId: string): ReaderProgress | null {
  try {
    const raw = localStorage.getItem(progressKey(seriesSlug, chapterId));
    if (!raw) return null;
    const value = JSON.parse(raw) as { page?: unknown; readingMode?: unknown; zoom?: unknown };
    return {
      page: typeof value.page === "number" ? value.page : 1,
      readingMode: value.readingMode as ReadingMode | undefined,
      zoom: typeof value.zoom === "number" ? value.zoom : undefined,
    };
  } catch {
    return null;
  }
}

export function saveLocalProgress(
  seriesSlug: string,
  chapterId: string,
  progress: ReaderProgress,
): void {
  localStorage.setItem(
    progressKey(seriesSlug, chapterId),
    JSON.stringify({ ...progress, timestamp: Date.now() }),
  );
}

export async function loadServerProgress(seriesSlug: string, chapterId: string): Promise<number | null> {
  try {
    const data = await api.get<{ last_page: number | null }>(`/progress/${seriesSlug}/${chapterId}`);
    return data.last_page == null ? null : data.last_page + 1;
  } catch {
    return null;
  }
}

export async function saveServerProgress(
  seriesSlug: string,
  chapterId: string,
  page: number,
): Promise<void> {
  try {
    await api.post(`/progress/${seriesSlug}/${chapterId}`, {
      chapter_uuid: chapterId,
      last_page: Math.max(0, page - 1),
    });
  } catch {
    // Local progress remains available when optional server sync fails.
  }
}
