import { afterEach, describe, expect, it, vi } from "vitest";
import nextConfig, { resolveApiInternalUrl, resolveMediaCspOrigins } from "../../../next.config";

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

  it("allows only configured HTTPS media origins", () => {
    expect(resolveMediaCspOrigins(undefined)).toEqual(
      process.env.APP_ENV === "development"
        ? ["http://host.docker.internal:*", "http://127.0.0.1:*", "http://localhost:*"]
        : [],
    );
    expect(resolveMediaCspOrigins("https://media.example, https://cdn.example")).toEqual([
      "https://media.example",
      "https://cdn.example",
    ]);
    expect(() => resolveMediaCspOrigins("https://media.example/path")).toThrow(
      "MEDIA_CSP_ORIGINS",
    );
    expect(() => resolveMediaCspOrigins("http://media.example")).toThrow("MEDIA_CSP_ORIGINS");
    expect(() => resolveMediaCspOrigins("https://*.r2.cloudflarestorage.com")).toThrow("MEDIA_CSP_ORIGINS");
    expect(resolveMediaCspOrigins("http://host.docker.internal:*")).toEqual([
      "http://host.docker.internal:*",
    ]);
  });

  it("defines a no-eval baseline security policy", async () => {
    const headers = await nextConfig.headers?.();
    const policy = headers?.[0]?.headers.find((header) => header.key === "Content-Security-Policy")?.value;
    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).not.toContain("unsafe-eval");
    expect(policy).toContain("connect-src 'self'");
  });
});
