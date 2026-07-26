const PAGE_IMAGE_CACHE_LIMIT = 250;
const pageImageCache = new Map<string, string>();

export function cachedPageImage(pageId: string): string | null {
  const source = pageImageCache.get(pageId);
  if (!source) return null;
  pageImageCache.delete(pageId);
  pageImageCache.set(pageId, source);
  return source;
}

export function cachePageImage(pageId: string, source: string): string {
  const existing = cachedPageImage(pageId);
  if (existing) {
    URL.revokeObjectURL(source);
    return existing;
  }
  pageImageCache.set(pageId, source);
  while (pageImageCache.size > PAGE_IMAGE_CACHE_LIMIT) {
    const oldest = pageImageCache.entries().next().value as [string, string] | undefined;
    if (!oldest) break;
    pageImageCache.delete(oldest[0]);
    URL.revokeObjectURL(oldest[1]);
  }
  return source;
}

export function evictCachedPageImage(pageId: string, expectedSource?: string): void {
  const source = pageImageCache.get(pageId);
  if (!source || (expectedSource && source !== expectedSource)) return;
  pageImageCache.delete(pageId);
  URL.revokeObjectURL(source);
}

export function clearReaderPageImageCache(): void {
  for (const source of pageImageCache.values()) URL.revokeObjectURL(source);
  pageImageCache.clear();
}
