import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api, ApiError, safeRedirect, getReturnUrl } from "@/lib/api";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

function okJson(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: () => Promise.resolve(data),
    text: () => Promise.resolve(JSON.stringify(data)),
  } as unknown as Response;
}

function errorJson(detail: string, status = 400): Response {
  return okJson({ detail }, status);
}

beforeEach(() => {
  mockFetch.mockReset();
  // Clear any cookies set by previous tests (jsdom requires explicit expiry)
  document.cookie.split(";").forEach((c) => {
    const name = c.trim().split("=")[0];
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ApiError", () => {
  it("has status and detail properties", () => {
    const err = new ApiError(422, "Invalid input");
    expect(err.status).toBe(422);
    expect(err.detail).toBe("Invalid input");
    expect(err.name).toBe("ApiError");
    expect(err).toBeInstanceOf(Error);
  });
});

describe("credentials: include on all requests", () => {
  it("GET includes credentials: include", async () => {
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.get("/test");
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/test",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("POST includes credentials: include", async () => {
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.post("/test", { a: 1 });
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/test",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("CSRF header behavior", () => {
  it("includes X-CSRF-Token on POST when cookie exists", async () => {
    document.cookie = "is_csrf=abc123";
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.post("/test");
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-CSRF-Token"]).toBe("abc123");
  });

  it("includes X-CSRF-Token on PUT when cookie exists", async () => {
    document.cookie = "is_csrf=xyz";
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.put("/test", { a: 1 });
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-CSRF-Token"]).toBe("xyz");
  });

  it("includes X-CSRF-Token on PATCH when cookie exists", async () => {
    document.cookie = "is_csrf=pqr";
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.patch("/test", { a: 1 });
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-CSRF-Token"]).toBe("pqr");
  });

  it("includes X-CSRF-Token on DELETE when cookie exists", async () => {
    document.cookie = "is_csrf=del";
    mockFetch.mockResolvedValueOnce(okJson(null, 204));
    await api.delete("/test");
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-CSRF-Token"]).toBe("del");
  });

  it("does NOT include X-CSRF-Token on GET", async () => {
    document.cookie = "is_csrf=abc123";
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.get("/test");
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("does NOT include X-CSRF-Token when cookie is absent", async () => {
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.post("/test", { a: 1 });
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["X-CSRF-Token"]).toBeUndefined();
  });
});

describe("Content-Type header behavior", () => {
  it("GET does not include Content-Type", async () => {
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.get("/test");
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["Content-Type"]).toBeUndefined();
  });

  it("POST includes Content-Type: application/json", async () => {
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.post("/test", { a: 1 });
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["Content-Type"]).toBe("application/json");
  });

  it("POST with no body does not set Content-Type", async () => {
    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.post("/test");
    const [, opts] = mockFetch.mock.calls[0];
    expect(opts.headers["Content-Type"]).toBeUndefined();
  });
});

describe("Refresh coordination", () => {
  it("attempts one refresh then retries on 401 (non-login, non-refresh)", async () => {
    mockFetch
      .mockResolvedValueOnce(errorJson("Unauthorized", 401)) // initial → 401
      .mockResolvedValueOnce(okJson({ ok: true }))           // refresh → ok
      .mockResolvedValueOnce(okJson({ ok: true }));          // retry → ok
    const result = await api.get("/protected");
    expect(mockFetch).toHaveBeenCalledTimes(3);
    expect(mockFetch.mock.calls[1][0]).toBe("/api/auth/refresh");
    expect(mockFetch.mock.calls[2][0]).toBe("/api/protected");
    expect(result).toEqual({ ok: true });
  });

  it("does NOT refresh on 401 from /auth/login", async () => {
    mockFetch.mockResolvedValueOnce(errorJson("Unauthorized", 401));
    await expect(api.post("/auth/login", { username: "a", password: "b" })).rejects.toThrow(
      "Unauthorized",
    );
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("does NOT refresh on 401 from /auth/refresh", async () => {
    mockFetch.mockResolvedValueOnce(errorJson("Unauthorized", 401));
    await expect(api.post("/auth/refresh")).rejects.toThrow("Unauthorized");
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("throws ApiError(401, 'Session expired') when refresh fails", async () => {
    mockFetch
      .mockResolvedValueOnce(errorJson("Unauthorized", 401)) // initial → 401
      .mockResolvedValueOnce(errorJson("Refresh failed", 401)); // refresh → fail
    await expect(api.get("/protected")).rejects.toMatchObject({
      status: 401,
      detail: "Session expired",
    });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("shares the same refresh promise for concurrent 401s (deduplication)", async () => {
    let resolveRefresh!: (v: Response) => void;
    const refreshResponse = new Promise<Response>((r) => {
      resolveRefresh = r;
    });

    // /a → 401, /b → 401, /auth/refresh → pending, then /a retry, /b retry
    mockFetch
      .mockResolvedValueOnce(errorJson("Unauthorized", 401))  // /a initial
      .mockResolvedValueOnce(errorJson("Unauthorized", 401))  // /b initial
      .mockReturnValueOnce(refreshResponse)                    // /auth/refresh (controlled)
      .mockResolvedValueOnce(okJson({ a: 1 }))                // /a retry
      .mockResolvedValueOnce(okJson({ b: 2 }));               // /b retry

    const p1 = api.get("/a");
    const p2 = api.get("/b");

    // Wait for both to hit 401 and start refresh
    await new Promise((r) => setTimeout(r, 20));

    // Only one refresh call should have been made
    const refreshCalls = mockFetch.mock.calls.filter(
      (c) => typeof c[0] === "string" && c[0].includes("/auth/refresh"),
    );
    expect(refreshCalls).toHaveLength(1);

    // Resolve the refresh
    resolveRefresh(okJson({ ok: true }));

    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toEqual({ a: 1 });
    expect(r2).toEqual({ b: 2 });
    expect(mockFetch).toHaveBeenCalledTimes(5);
  });
});

describe("No tokens in client", () => {
  it("api module never reads/writes localStorage", async () => {
    const getSpy = vi.spyOn(Storage.prototype, "getItem");
    const setSpy = vi.spyOn(Storage.prototype, "setItem");

    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.get("/test");

    expect(getSpy).not.toHaveBeenCalled();
    expect(setSpy).not.toHaveBeenCalled();

    getSpy.mockRestore();
    setSpy.mockRestore();
  });

  it("api module never reads/writes sessionStorage", async () => {
    const getSpy = vi.spyOn(sessionStorage, "getItem");
    const setSpy = vi.spyOn(sessionStorage, "setItem");

    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.get("/test");

    expect(getSpy).not.toHaveBeenCalled();
    expect(setSpy).not.toHaveBeenCalled();

    getSpy.mockRestore();
    setSpy.mockRestore();
  });

  it("api module never reads access/refresh cookies directly", async () => {
    document.cookie = "access_token=secret; refresh_token=secret2; is_csrf=abc";
    const cookieSpy = vi.spyOn(document, "cookie", "get");

    mockFetch.mockResolvedValueOnce(okJson({ ok: true }));
    await api.get("/test");

    // The only cookie access should be for is_csrf via readCsrfCookie
    // which uses a regex on document.cookie — it should never read
    // access_token or refresh_token by name
    const cookieGetCalls = cookieSpy.mock.calls as unknown as Array<[string]>;
    for (const call of cookieGetCalls) {
      expect(call).not.toMatch(/access_token/);
      expect(call).not.toMatch(/refresh_token/);
    }

    cookieSpy.mockRestore();
  });
});

describe("safeRedirect", () => {
  it("allows /series/foo", () => {
    expect(safeRedirect("/series/foo")).toBe("/series/foo");
  });

  it("allows /", () => {
    expect(safeRedirect("/")).toBe("/");
  });

  it("blocks //evil.com", () => {
    expect(safeRedirect("//evil.com")).toBe("/");
  });

  it("blocks http://evil.com", () => {
    expect(safeRedirect("http://evil.com")).toBe("/");
  });

  it("blocks https://evil.com", () => {
    expect(safeRedirect("https://evil.com")).toBe("/");
  });

  it("blocks relative path without leading slash", () => {
    expect(safeRedirect("evil.com")).toBe("/");
  });
});

describe("getReturnUrl", () => {
  it("reads return search param", () => {
    const params = new URLSearchParams("return=/series/manga");
    expect(getReturnUrl(params)).toBe("/series/manga");
  });

  it("defaults to / when no return param", () => {
    const params = new URLSearchParams();
    expect(getReturnUrl(params)).toBe("/");
  });

  it("blocks unsafe return URLs", () => {
    const params = new URLSearchParams("return=https://evil.com");
    expect(getReturnUrl(params)).toBe("/");
  });
});
