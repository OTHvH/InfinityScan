import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import {
  ReaderProgressController,
  attachProgressFlushListeners,
  chooseNewestProgress,
  clampProgress,
  loadLocalProgress,
  saveLocalProgress,
  saveServerProgress,
  type ReaderProgress,
} from "../progress";

const storage = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    clear: () => storage.clear(),
    getItem: (key: string) => storage.get(key) ?? null,
    removeItem: (key: string) => storage.delete(key),
    setItem: (key: string, value: string) => storage.set(key, value),
  },
});

vi.mock("@/lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

function progress(overrides: Partial<ReaderProgress> = {}): ReaderProgress {
  return {
    seriesSlug: "series-a",
    chapterId: "chapter-a",
    page: 4,
    scrollRatio: 0.5,
    updatedAt: 1_000,
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

afterEach(() => vi.useRealTimers());

describe("progress persistence", () => {
  it("keys guest progress with series and chapter IDs", () => {
    saveLocalProgress(progress());
    expect(loadLocalProgress("series-a", "chapter-a")).toEqual(progress());
    expect(loadLocalProgress("series-a", "4")).toBeNull();
  });

  it("sends authenticated progress without a user ID", async () => {
    vi.mocked(api.post).mockResolvedValue(undefined);
    await saveServerProgress(progress());

    expect(api.post).toHaveBeenCalledWith(
      "/progress/series-a/chapter-a",
      {
        chapter_uuid: "chapter-a",
        last_page: 3,
        scroll_position: 0.5,
      },
    );
    expect(vi.mocked(api.post).mock.calls[0][1]).not.toHaveProperty("user_id");
  });

  it("uses keepalive for an authenticated pagehide save", async () => {
    vi.mocked(api.post).mockResolvedValue(undefined);
    await saveServerProgress(progress(), { keepalive: true });
    expect(api.post).toHaveBeenCalledWith(
      "/progress/series-a/chapter-a",
      expect.any(Object),
      { keepalive: true },
    );
  });
});

describe("progress save coordination", () => {
  it("debounces normal guest writes", async () => {
    vi.useFakeTimers();
    const saveLocal = vi.fn();
    const statuses: string[] = [];
    const controller = new ReaderProgressController({
      seriesSlug: "series-a",
      authenticated: false,
      debounceMs: 100,
      onStatus: (status) => statuses.push(status),
      saveLocal,
    });
    controller.update(progress({ page: 1 }));
    controller.update(progress({ page: 2 }));
    await vi.advanceTimersByTimeAsync(99);
    expect(saveLocal).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(saveLocal).toHaveBeenCalledOnce();
    expect(saveLocal).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
    expect(statuses.at(-1)).toBe("saved locally");
  });

  it("flushes the previous visible chapter on transition", async () => {
    vi.useFakeTimers();
    const saveLocal = vi.fn();
    const controller = new ReaderProgressController({
      seriesSlug: "series-a",
      authenticated: false,
      debounceMs: 100,
      onStatus: vi.fn(),
      saveLocal,
    });
    controller.update(progress({ chapterId: "chapter-a" }));
    controller.update(progress({ chapterId: "chapter-b", page: 1 }));
    await Promise.resolve();
    expect(saveLocal).toHaveBeenCalledWith(expect.objectContaining({ chapterId: "chapter-a" }));
    expect(saveLocal).not.toHaveBeenCalledWith(expect.objectContaining({ chapterId: "chapter-b" }));
  });

  it("flushes on pagehide with keepalive and again on unmount when dirty", async () => {
    vi.useFakeTimers();
    const saveServer = vi.fn().mockResolvedValue(undefined);
    const first = new ReaderProgressController({
      seriesSlug: "series-a",
      authenticated: true,
      onStatus: vi.fn(),
      saveLocal: vi.fn(),
      saveServer,
    });
    first.update(progress());
    const detachFirst = attachProgressFlushListeners(first);
    window.dispatchEvent(new PageTransitionEvent("pagehide"));
    await Promise.resolve();
    expect(saveServer).toHaveBeenCalledWith(progress(), { keepalive: true });
    detachFirst();

    const unmountSave = vi.fn().mockResolvedValue(undefined);
    const second = new ReaderProgressController({
      seriesSlug: "series-a",
      authenticated: true,
      onStatus: vi.fn(),
      saveLocal: vi.fn(),
      saveServer: unmountSave,
    });
    second.update(progress({ page: 5 }));
    const detachSecond = attachProgressFlushListeners(second);
    detachSecond();
    await Promise.resolve();
    expect(unmountSave).toHaveBeenCalledWith(expect.objectContaining({ page: 5 }), {});
  });

  it("makes only one retry after a temporary authenticated failure", async () => {
    vi.useFakeTimers();
    const statuses: string[] = [];
    const saveServer = vi.fn().mockRejectedValue(new Error("offline"));
    const controller = new ReaderProgressController({
      seriesSlug: "series-a",
      authenticated: true,
      debounceMs: 10,
      retryMs: 20,
      onStatus: (status) => statuses.push(status),
      saveLocal: vi.fn(),
      saveServer,
    });
    controller.update(progress());
    await vi.advanceTimersByTimeAsync(10);
    expect(saveServer).toHaveBeenCalledOnce();
    expect(statuses.at(-1)).toBe("saved locally");
    await vi.advanceTimersByTimeAsync(20);
    expect(saveServer).toHaveBeenCalledTimes(2);
    expect(statuses.at(-1)).toBe("retry required");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(saveServer).toHaveBeenCalledTimes(2);
  });
});

describe("progress resume selection", () => {
  it("uses newer valid server progress and resumes a middle page", () => {
    const local = progress({ page: 2, updatedAt: 1_000 });
    const server = progress({ page: 7, updatedAt: 2_000 });
    expect(chooseNewestProgress(server, local, "series-a", "chapter-a")?.page).toBe(7);
  });

  it("ignores stale and mismatched progress", () => {
    const local = progress({ page: 6, updatedAt: 2_000 });
    const staleServer = progress({ page: 3, updatedAt: 1_000 });
    const wrongChapter = progress({ chapterId: "chapter-b", page: 9, updatedAt: 3_000 });
    expect(chooseNewestProgress(staleServer, local, "series-a", "chapter-a")?.page).toBe(6);
    expect(chooseNewestProgress(wrongChapter, local, "series-a", "chapter-a")?.page).toBe(6);
  });

  it("clamps an invalid page to chapter bounds", () => {
    expect(clampProgress(progress({ page: 999 }), 12).page).toBe(12);
    expect(clampProgress(progress({ page: 1 }), 12).page).toBe(1);
  });
});
