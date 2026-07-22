import type { ReaderChapter, ReaderDirection } from "./types";

export function getAdjacentChapterId(
  chapter: ReaderChapter,
  direction: ReaderDirection,
): string | null {
  return direction === "previous" ? chapter.previousChapterId : chapter.nextChapterId;
}

export function getLoadedAdjacentChapterId(
  activeChapterId: string,
  orderedChapterIds: string[],
  direction: ReaderDirection,
): string | null {
  const activeIndex = orderedChapterIds.indexOf(activeChapterId);
  if (activeIndex < 0) return null;
  return orderedChapterIds[activeIndex + (direction === "previous" ? -1 : 1)] ?? null;
}
