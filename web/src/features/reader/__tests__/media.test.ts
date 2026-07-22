import { describe, expect, it } from "vitest";
import { resolveMediaUrl } from "@/lib/api";

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
});
