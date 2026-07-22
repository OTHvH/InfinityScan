import { describe, expect, it } from "vitest";
import { resolveMediaUrl, resolvePageMediaRequest } from "@/lib/api";

describe("reader media URL resolution", () => {
  it("resolves an expected root-relative media path against the API origin", () => {
    expect(resolveMediaUrl("/media/pages/page-id")).toBe("http://localhost:3000/media/pages/page-id");
  });

  it.each([
    "https://storage.example/page.jpg",
    "//storage.example/page.jpg",
    "/media/pages/page-id?signature=secret",
    "/media/covers/cover-id",
  ])("rejects unsafe media path %s", (path) => {
    expect(() => resolveMediaUrl(path)).toThrow("Invalid reader media path");
  });

  it("adds retry attempts only to the resolved API media endpoint", () => {
    expect(resolvePageMediaRequest("/media/pages/page-id", 2))
      .toBe("http://localhost:3000/media/pages/page-id?attempt=2");
  });

  it.each([
    "https://storage.example/page.jpg",
    "http://localhost:3000/media/pages/page-id?signature=secret",
    "http://localhost:3000/media/pages/page-id/extra",
  ])("rejects an unsafe media request URL %s", (path) => {
    expect(() => resolvePageMediaRequest(path, 2)).toThrow("Invalid reader media path");
  });
});
