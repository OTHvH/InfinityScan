import { api } from "@/lib/api";
import type { ReadingMode } from "./types";

export type ProgressStatus = "saved" | "saving" | "saved locally" | "retry required";

export interface ReaderProgress {
  seriesSlug: string;
  chapterId: string;
  page: number;
  scrollRatio: number;
  updatedAt: number;
  readingMode?: ReadingMode;
  zoom?: number;
}

interface RawServerProgress {
  chapter_uuid: string;
  last_page: number | null;
  scroll_position: number | null;
  updated_at: string | null;
}

export interface ProgressSaveOptions {
  keepalive?: boolean;
}

export interface ProgressControllerOptions {
  seriesSlug: string;
  authenticated: boolean;
  debounceMs?: number;
  retryMs?: number;
  onStatus: (status: ProgressStatus) => void;
  saveLocal?: typeof saveLocalProgress;
  saveServer?: typeof saveServerProgress;
}

function progressKey(seriesSlug: string, chapterId: string): string {
  return `infinityscan_progress_${seriesSlug}_${chapterId}`;
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isValidProgress(
  progress: ReaderProgress | null,
  seriesSlug: string,
  chapterId: string,
): progress is ReaderProgress {
  return !!progress
    && progress.seriesSlug === seriesSlug
    && progress.chapterId === chapterId
    && Number.isInteger(progress.page)
    && progress.page >= 1
    && finiteNumber(progress.scrollRatio)
    && progress.scrollRatio >= 0
    && progress.scrollRatio <= 1
    && finiteNumber(progress.updatedAt)
    && progress.updatedAt > 0;
}

export function loadLocalProgress(seriesSlug: string, chapterId: string): ReaderProgress | null {
  try {
    const raw = localStorage.getItem(progressKey(seriesSlug, chapterId));
    if (!raw) return null;
    const value = JSON.parse(raw) as Record<string, unknown>;
    const progress: ReaderProgress = {
      seriesSlug: typeof value.seriesSlug === "string" ? value.seriesSlug : seriesSlug,
      chapterId: typeof value.chapterId === "string" ? value.chapterId : chapterId,
      page: finiteNumber(value.page) ? value.page : 1,
      scrollRatio: finiteNumber(value.scrollRatio)
        ? value.scrollRatio
        : finiteNumber(value.scroll_position) ? value.scroll_position : 0,
      updatedAt: finiteNumber(value.updatedAt)
        ? value.updatedAt
        : finiteNumber(value.timestamp) ? value.timestamp : 0,
      readingMode: value.readingMode === "scroll"
        ? "continuous"
        : value.readingMode as ReadingMode | undefined,
      zoom: finiteNumber(value.zoom) ? value.zoom : undefined,
    };
    return isValidProgress(progress, seriesSlug, chapterId) ? progress : null;
  } catch {
    return null;
  }
}

export function saveLocalProgress(progress: ReaderProgress): void {
  localStorage.setItem(progressKey(progress.seriesSlug, progress.chapterId), JSON.stringify(progress));
}

export async function loadServerProgress(seriesSlug: string, chapterId: string): Promise<ReaderProgress | null> {
  try {
    const data = await api.get<RawServerProgress>(
      `/progress/${encodeURIComponent(seriesSlug)}/${encodeURIComponent(chapterId)}`,
    );
    if (data.last_page == null || data.updated_at == null) return null;
    const progress: ReaderProgress = {
      seriesSlug,
      chapterId: data.chapter_uuid,
      page: data.last_page + 1,
      scrollRatio: data.scroll_position ?? 0,
      updatedAt: Date.parse(data.updated_at),
    };
    return isValidProgress(progress, seriesSlug, chapterId) ? progress : null;
  } catch {
    return null;
  }
}

export async function saveServerProgress(
  progress: ReaderProgress,
  options: ProgressSaveOptions = {},
): Promise<void> {
  const path = `/progress/${encodeURIComponent(progress.seriesSlug)}/${encodeURIComponent(progress.chapterId)}`;
  const body = {
    chapter_uuid: progress.chapterId,
    last_page: Math.max(0, progress.page - 1),
    scroll_position: progress.scrollRatio,
  };
  if (options.keepalive) await api.post(path, body, { keepalive: true });
  else await api.post(path, body);
}

export function chooseNewestProgress(
  server: ReaderProgress | null,
  local: ReaderProgress | null,
  seriesSlug: string,
  chapterId: string,
): ReaderProgress | null {
  const validServer = isValidProgress(server, seriesSlug, chapterId) ? server : null;
  const validLocal = isValidProgress(local, seriesSlug, chapterId) ? local : null;
  if (!validServer) return validLocal;
  if (!validLocal) return validServer;
  return validServer.updatedAt >= validLocal.updatedAt ? validServer : validLocal;
}

export function clampProgress(progress: ReaderProgress, pageCount: number): ReaderProgress {
  const boundedCount = Math.max(1, pageCount);
  return {
    ...progress,
    page: Math.min(Math.max(1, progress.page), boundedCount),
    scrollRatio: Math.min(1, Math.max(0, progress.scrollRatio)),
  };
}

function samePosition(left: ReaderProgress | null, right: ReaderProgress): boolean {
  return !!left
    && left.seriesSlug === right.seriesSlug
    && left.chapterId === right.chapterId
    && left.page === right.page
    && left.scrollRatio === right.scrollRatio
    && left.readingMode === right.readingMode
    && left.zoom === right.zoom;
}

export class ReaderProgressController {
  private readonly debounceMs: number;
  private readonly retryMs: number;
  private readonly onStatus: (status: ProgressStatus) => void;
  private readonly saveLocal: typeof saveLocalProgress;
  private readonly saveServer: typeof saveServerProgress;
  private authenticated: boolean;
  private latest: ReaderProgress | null = null;
  private saved: ReaderProgress | null = null;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryProgress: ReaderProgress | null = null;
  private disposed = false;

  constructor(options: ProgressControllerOptions) {
    this.authenticated = options.authenticated;
    this.debounceMs = options.debounceMs ?? 1500;
    this.retryMs = options.retryMs ?? 2000;
    this.onStatus = options.onStatus;
    this.saveLocal = options.saveLocal ?? saveLocalProgress;
    this.saveServer = options.saveServer ?? saveServerProgress;
  }

  setAuthenticated(authenticated: boolean): void {
    this.authenticated = authenticated;
  }

  update(progress: ReaderProgress): void {
    if (this.disposed || samePosition(this.latest, progress)) return;
    const previous = this.latest;
    this.latest = progress;
    if (previous && previous.chapterId !== progress.chapterId) {
      this.clearDebounce();
      void this.persist(previous, true);
    }
    this.clearDebounce();
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      void this.persist(progress, true);
    }, this.debounceMs);
  }

  flush(options: ProgressSaveOptions = {}): Promise<void> {
    this.clearDebounce();
    if (!this.latest || samePosition(this.saved, this.latest)) return Promise.resolve();
    return this.persist(this.latest, true, options);
  }

  dispose(): void {
    this.disposed = true;
    this.clearDebounce();
    this.clearRetry();
  }

  private async persist(
    progress: ReaderProgress,
    allowRetry: boolean,
    options: ProgressSaveOptions = {},
  ): Promise<void> {
    const isLatest = () => this.latest?.chapterId === progress.chapterId;
    try {
      this.saveLocal(progress);
    } catch {
      if (!this.authenticated && isLatest()) this.onStatus("retry required");
      if (!this.authenticated) return;
    }
    if (!this.authenticated) {
      this.saved = progress;
      if (isLatest()) this.onStatus("saved locally");
      return;
    }

    if (isLatest()) this.onStatus("saving");
    try {
      await this.saveServer(progress, options);
      this.saved = progress;
      if (this.retryProgress?.chapterId === progress.chapterId) this.clearRetry();
      if (isLatest()) this.onStatus("saved");
    } catch {
      if (isLatest()) this.onStatus(allowRetry ? "saved locally" : "retry required");
      if (!allowRetry || this.retryTimer || this.disposed) return;
      this.retryProgress = progress;
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        const retryProgress = this.retryProgress;
        this.retryProgress = null;
        if (retryProgress) void this.persist(retryProgress, false);
      }, this.retryMs);
    }
  }

  private clearDebounce(): void {
    if (this.debounceTimer) clearTimeout(this.debounceTimer);
    this.debounceTimer = null;
  }

  private clearRetry(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.retryProgress = null;
  }
}

export function attachProgressFlushListeners(
  controller: ReaderProgressController,
  targetDocument: Document = document,
  targetWindow: Window = window,
): () => void {
  const onVisibilityChange = () => {
    if (targetDocument.visibilityState === "hidden") void controller.flush();
  };
  const onPageHide = () => { void controller.flush({ keepalive: true }); };
  targetDocument.addEventListener("visibilitychange", onVisibilityChange);
  targetWindow.addEventListener("pagehide", onPageHide);
  return () => {
    targetDocument.removeEventListener("visibilitychange", onVisibilityChange);
    targetWindow.removeEventListener("pagehide", onPageHide);
    void controller.flush();
    controller.dispose();
  };
}
