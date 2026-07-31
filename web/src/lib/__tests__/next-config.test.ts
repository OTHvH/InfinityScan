import { afterEach, describe, expect, it, vi } from "vitest";
import nextConfig, { resolveApiInternalUrl } from "../../../next.config";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("API_INTERNAL_URL validation", () => {
  it("defaults only in development", () => {
    vi.stubEnv("API_INTERNAL_URL", "http://ambient-api:8000");
    expect(resolveApiInternalUrl(undefined, "development")).toBe("http://localhost:8000");
    expect(() => resolveApiInternalUrl(undefined, "production")).toThrow(
      "API_INTERNAL_URL is required outside development",
    );
  });

  it.each([
    "ftp://api:8000",
    "http://user:password@api:8000",
    "http://api:8000/backend",
    "http://api:8000?debug=true",
    "http://api:8000#fragment",
  ])("rejects unsafe internal URL %s", (url) => {
    expect(() => resolveApiInternalUrl(url, "production")).toThrow(
      "API_INTERNAL_URL must be an absolute HTTP(S) origin",
    );
  });

  it("returns a canonical HTTP(S) origin", () => {
    expect(resolveApiInternalUrl("http://api:8000/", "production")).toBe("http://api:8000");
  });

  it("strips the public API prefix when proxying to FastAPI", async () => {
    vi.stubEnv("API_INTERNAL_URL", "http://api:8000");
    await expect(nextConfig.rewrites?.()).resolves.toEqual([
      {
        source: "/api/:path*",
        destination: "http://api:8000/:path*",
      },
    ]);
  });
});
