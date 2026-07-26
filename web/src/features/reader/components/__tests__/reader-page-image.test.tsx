import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReaderPage } from "../ReaderPage";
import { clearReaderPageImageCache } from "../../media-cache";
import type { ReaderItem } from "../../types";

function page(overrides: Partial<Extract<ReaderItem, { kind: "page" }>> = {}): Extract<ReaderItem, { kind: "page" }> {
  return {
    kind: "page",
    key: "page:page-id",
    chapterId: "chapter-id",
    chapterNumber: "7.5",
    pageId: "page-id",
    pageNumber: 3,
    mediaPath: "/media/pages/page-id",
    width: 800,
    height: 1200,
    aspectRatio: 2 / 3,
    ...overrides,
  };
}

function mediaResponse(status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    blob: vi.fn().mockResolvedValue(new Blob(["image"], { type: "image/jpeg" })),
  } as unknown as Response;
}

async function flushPromises(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

let fetchMock: ReturnType<typeof vi.fn>;
let decodeMock: ReturnType<typeof vi.fn>;
let objectUrlIndex: number;

beforeEach(() => {
  objectUrlIndex = 0;
  fetchMock = vi.fn();
  decodeMock = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("fetch", fetchMock);
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => `blob:reader-page-${++objectUrlIndex}`),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(HTMLImageElement.prototype, "decode", {
    configurable: true,
    value: decodeMock,
  });
});

afterEach(() => {
  clearReaderPageImageCache();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ReaderPage resilient image loading", () => {
  it("reserves the page aspect ratio and keeps the skeleton before loading", () => {
    fetchMock.mockReturnValue(new Promise(() => undefined));
    render(<ReaderPage item={page()} zoom={100} fitWidth />);

    const image = screen.getByRole("img", { name: "Chapter 7.5, page 3" });
    expect(image.parentElement).toHaveStyle({ aspectRatio: "800 / 1200", width: "100%" });
    expect(image.parentElement).toHaveAttribute("data-image-state", "loading");
    expect(image.parentElement?.querySelector(".reader-page-skeleton")).toBeInTheDocument();
    expect(image).toHaveAttribute("width", "800");
    expect(image).toHaveAttribute("height", "1200");
  });

  it("loads only the InfinityScan media endpoint into a blob URL", async () => {
    fetchMock.mockResolvedValue(mediaResponse());
    const consoleSpy = vi.spyOn(console, "log");
    render(<ReaderPage item={page()} zoom={100} fitWidth />);

    await waitFor(() => expect(screen.getByRole("img")).toHaveAttribute("src", "blob:reader-page-1"));
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:3000/media/pages/page-id",
      expect.objectContaining({ credentials: "omit", signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByRole("img").getAttribute("src")).not.toContain("objects.");
    expect(consoleSpy).not.toHaveBeenCalled();
  });

  it("fades in only after browser decoding succeeds", async () => {
    fetchMock.mockResolvedValue(mediaResponse());
    render(<ReaderPage item={page()} zoom={100} fitWidth />);
    const image = screen.getByRole("img");
    await waitFor(() => expect(image).toHaveAttribute("src", "blob:reader-page-1"));

    fireEvent.load(image);
    await waitFor(() => expect(image.parentElement).toHaveAttribute("data-image-state", "loaded"));
    expect(decodeMock).toHaveBeenCalledOnce();
    expect(image).toHaveClass("loaded");
    expect(image.parentElement?.querySelector(".reader-page-skeleton")).not.toBeInTheDocument();
  });

  it("reuses a cached blob URL when virtualization remounts the page", async () => {
    fetchMock.mockResolvedValue(mediaResponse());
    const first = render(<ReaderPage item={page()} zoom={100} fitWidth />);
    await waitFor(() => expect(screen.getByRole("img")).toHaveAttribute("src", "blob:reader-page-1"));
    first.unmount();

    render(<ReaderPage item={page()} zoom={100} fitWidth />);
    await waitFor(() => expect(screen.getByRole("img")).toHaveAttribute("src", "blob:reader-page-1"));
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(URL.revokeObjectURL).not.toHaveBeenCalledWith("blob:reader-page-1");
  });

  it("automatically retries a temporary failure and then succeeds", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValueOnce(mediaResponse());
    render(<ReaderPage item={page()} zoom={100} fitWidth />);
    await flushPromises();
    expect(screen.getByRole("status")).toHaveTextContent("Temporary image loading failure");

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("http://localhost:3000/media/pages/page-id?attempt=2");
    expect(screen.getByRole("img")).toHaveAttribute("src", "blob:reader-page-1");
  });

  it("retries an active AbortError that was not caused by its own signal", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockRejectedValueOnce(new DOMException("transport interrupted", "AbortError"))
      .mockResolvedValueOnce(mediaResponse());
    render(<ReaderPage item={page()} zoom={100} fitWidth />);
    await flushPromises();
    expect(screen.getByRole("status")).toHaveTextContent("Temporary image loading failure");

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("http://localhost:3000/media/pages/page-id?attempt=2");
  });

  it("treats a media endpoint 404 as unavailable without retrying", async () => {
    fetchMock.mockResolvedValue(mediaResponse(404));
    render(<ReaderPage item={page()} zoom={100} fitWidth />);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Page unavailable or not verified"));
    expect(screen.getByRole("img").parentElement).toHaveAttribute("data-image-state", "unavailable");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("distinguishes decode failure and preserves the reserved height", async () => {
    decodeMock.mockRejectedValue(new DOMException("bad image", "EncodingError"));
    fetchMock.mockResolvedValue(mediaResponse());
    render(<ReaderPage item={page()} zoom={100} fitWidth />);
    const image = screen.getByRole("img");
    await waitFor(() => expect(image).toHaveAttribute("src", "blob:reader-page-1"));
    fireEvent.load(image);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Page image could not be decoded"));
    expect(image.parentElement).toHaveStyle({ aspectRatio: "800 / 1200" });
    expect(image.parentElement?.querySelector(".reader-page-skeleton")).toBeInTheDocument();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:reader-page-1");
  });

  it("allows an immediate manual retry using the same endpoint", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockRejectedValueOnce(new TypeError("temporary"))
      .mockResolvedValueOnce(mediaResponse());
    render(<ReaderPage item={page()} zoom={100} fitWidth />);
    await flushPromises();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe("http://localhost:3000/media/pages/page-id?attempt=2");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops after three automatic retries", async () => {
    vi.useFakeTimers();
    fetchMock.mockRejectedValue(new TypeError("offline"));
    render(<ReaderPage item={page()} zoom={100} fitWidth />);
    await flushPromises();
    for (const delay of [500, 1000, 2000]) {
      await act(async () => { await vi.advanceTimersByTimeAsync(delay); });
      await flushPromises();
    }

    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "http://localhost:3000/media/pages/page-id",
      "http://localhost:3000/media/pages/page-id?attempt=2",
      "http://localhost:3000/media/pages/page-id?attempt=3",
      "http://localhost:3000/media/pages/page-id?attempt=4",
    ]);
    expect(screen.getByRole("img").parentElement).toHaveAttribute("data-image-state", "unavailable");
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("clears a pending retry timer on unmount", async () => {
    vi.useFakeTimers();
    fetchMock.mockRejectedValue(new TypeError("offline"));
    const { unmount } = render(<ReaderPage item={page()} zoom={100} fitWidth />);
    await flushPromises();
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("aborts an active request when unmounted", () => {
    let signal: AbortSignal | undefined;
    fetchMock.mockImplementation((_url, options: RequestInit) => {
      signal = options.signal as AbortSignal;
      return new Promise(() => undefined);
    });
    const { unmount } = render(<ReaderPage item={page()} zoom={100} fitWidth />);
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("stops active work when the feed evicts the page", () => {
    let evictionCleanup: (() => void) | undefined;
    let signal: AbortSignal | undefined;
    const registerCleanup = vi.fn((_pageId: string, cleanup: () => void) => {
      evictionCleanup = cleanup;
      return vi.fn();
    });
    fetchMock.mockImplementation((_url, options: RequestInit) => {
      signal = options.signal as AbortSignal;
      return new Promise(() => undefined);
    });
    render(<ReaderPage item={page()} zoom={100} fitWidth registerCleanup={registerCleanup} />);

    expect(registerCleanup).toHaveBeenCalledWith("page-id", expect.any(Function));
    act(() => evictionCleanup?.());
    expect(signal?.aborted).toBe(true);
  });
});
