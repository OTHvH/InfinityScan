import type { ReaderChapter, ReaderItem } from "./types";

export function buildReaderItems(
  chaptersById: Record<string, ReaderChapter>,
  orderedChapterIds: string[],
): ReaderItem[] {
  const items: ReaderItem[] = [];
  const seenPageIds = new Set<string>();
  for (const chapterId of orderedChapterIds) {
    const chapter = chaptersById[chapterId];
    if (!chapter) continue;
    items.push({
      kind: "chapter-separator",
      key: `chapter:${chapter.id}`,
      chapterId: chapter.id,
      chapterNumber: chapter.number,
      title: chapter.title,
    });
    for (const page of chapter.pages) {
      if (seenPageIds.has(page.id)) continue;
      seenPageIds.add(page.id);
      items.push({
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
      });
    }
  }
  return items;
}
