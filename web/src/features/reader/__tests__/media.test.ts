import { describe, expect, it } from "vitest";
import { resolveMediaUrl, resolvePageMediaRequest } from "@/lib/api";

describe("reader media URL resolution", () => {
  it("resolves a backend-relative page path through the same-origin API", () => {
    expect(resolveMediaUrl("/media/pages/page-id")).toBe("/api/media/pages/page-id");
  });

  it("resolves a backend-relative cover path through the same-origin API", () => {
    expect(resolveMediaUrl("/media/covers/cover-id")).toBe("/api/media/covers/cover-id");
  });

  it.each([
    "https://storage.example/page.jpg",
    "//storage.example/page.jpg",
    "/media/pages/page-id?signature=secret",
    "/covers/cover-id",
  ])("rejects unsafe media path %s", (path) => {
    expect(() => resolveMediaUrl(path)).toThrow("Invalid media path");
  });

  it("adds retry attempts only to the resolved API media endpoint", () => {
    expect(resolvePageMediaRequest("/media/pages/page-id", 2))
      .toBe("/api/media/pages/page-id?attempt=2");
  });

  it.each([
    "https://storage.example/page.jpg",
    "http://localhost:3000/api/media/pages/page-id?signature=secret",
    "http://localhost:3000/api/media/pages/page-id/extra",
  ])("rejects an unsafe media request URL %s", (path) => {
    expect(() => resolvePageMediaRequest(path, 2)).toThrow("Invalid reader media path");
  });
});
