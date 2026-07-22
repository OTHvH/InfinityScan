import { api, resolveMediaUrl } from "@/lib/api";
import type { ReaderChunkRequest, ReaderChapter, ReaderChunkResponse, ReaderPage, ReaderSeries } from "./types";

interface RawReaderPage {
  id: string;
  page_number: number;
  media_path: string;
  width: number | null;
  height: number | null;
  aspect_ratio: number | null;
}

interface RawReaderChapter {
  id: string;
  number: string;
  title: string | null;
  page_count: number;
  previous_chapter_id: string | null;
  next_chapter_id: string | null;
  pages: RawReaderPage[];
}

interface RawReaderChunkResponse {
  series: ReaderSeries;
  chapters: RawReaderChapter[];
  next_cursor: string | null;
  previous_cursor: string | null;
  has_more_next: boolean;
  has_more_previous: boolean;
}

function mapPage(page: RawReaderPage): ReaderPage {
  return {
    id: page.id,
    pageNumber: page.page_number,
    mediaPath: resolveMediaUrl(page.media_path),
    width: page.width ?? 0,
    height: page.height ?? 0,
    aspectRatio: page.aspect_ratio ?? 0,
  };
}

function mapChapter(chapter: RawReaderChapter): ReaderChapter {
  return {
    id: chapter.id,
    number: chapter.number,
    title: chapter.title,
    pageCount: chapter.page_count,
    previousChapterId: chapter.previous_chapter_id,
    nextChapterId: chapter.next_chapter_id,
    pages: chapter.pages.map(mapPage),
  };
}

export async function fetchReaderChunk(request: ReaderChunkRequest): Promise<ReaderChunkResponse> {
  const query = new URLSearchParams({
    direction: request.direction,
    limit: String(request.limit),
  });
  if (request.cursor) query.set("cursor", request.cursor);
  if (request.startChapterId) query.set("start_chapter_id", request.startChapterId);
  const response = await api.get<RawReaderChunkResponse>(
    `/reader/${encodeURIComponent(request.seriesSlug)}/chunks?${query.toString()}`,
    { signal: request.signal },
  );
  return {
    series: response.series,
    chapters: response.chapters.map(mapChapter),
    nextCursor: response.next_cursor,
    previousCursor: response.previous_cursor,
    hasMoreNext: response.has_more_next,
    hasMorePrevious: response.has_more_previous,
  };
}
