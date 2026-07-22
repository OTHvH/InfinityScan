/**
 * Central browser API client for InfinityScan.
 *
 * Every request uses `credentials: "include"` to send cookies.
 * Unsafe methods (POST/PUT/PATCH/DELETE) include the CSRF header.
 * 401 responses trigger a single refresh-and-retry.
 *
 * Never:
 *   - Read access or refresh cookies directly
 *   - Store tokens in localStorage / sessionStorage / state / URLs / logs
 *   - Expose backend stack traces
 *   - Hard-code production domains
 */

import type { User } from "./types";

// ── Base URL ─────────────────────────────────────────────────────────────────

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

const MEDIA_PATH_PREFIX = "/media/pages/";

/** Resolve only same-API, root-relative media paths returned by the server. */
export function resolveMediaUrl(path: string): string {
  if (
    typeof path !== "string" ||
    !path.startsWith(MEDIA_PATH_PREFIX) ||
    path.startsWith("//") ||
    path.includes("://") ||
    path.includes("?") ||
    path.includes("#")
  ) {
    throw new Error("Invalid reader media path");
  }
  if (API_BASE) return `${API_BASE.replace(/\/$/, "")}${path}`;
  if (typeof window !== "undefined") return `${window.location.origin}${path}`;
  return path;
}

// ── CSRF cookie reader ───────────────────────────────────────────────────────

function readCsrfCookie(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp("(^| )is_csrf=([^;]*)"));
  return match ? decodeURIComponent(match[2]) : null;
}

// ── Unsafe method check ──────────────────────────────────────────────────────

function isUnsafe(method: string): boolean {
  const m = method.toUpperCase();
  return m === "POST" || m === "PUT" || m === "PATCH" || m === "DELETE";
}

// ── Error types ──────────────────────────────────────────────────────────────

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// ── Response parser ──────────────────────────────────────────────────────────

async function parseResponse(res: Response): Promise<unknown> {
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    return res.json();
  }
  if (res.status === 204) return null;
  return res.text();
}

function extractDetail(body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as Record<string, unknown>).detail;
    if (typeof d === "string") return d;
  }
  return "Request failed";
}

// ── Refresh coordination ─────────────────────────────────────────────────────

let refreshPromise: Promise<boolean> | null = null;

function doRefresh(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;

  refreshPromise = (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: {
          "X-CSRF-Token": readCsrfCookie() ?? "",
        },
      });
      return res.ok;
    } catch {
      return false;
    } finally {
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

// ── Core fetch wrapper ───────────────────────────────────────────────────────

async function request<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> | undefined),
  };

  if (isUnsafe(method)) {
    const csrf = readCsrfCookie();
    if (csrf) {
      headers["X-CSRF-Token"] = csrf;
    }
    if (!headers["Content-Type"] && options.body) {
      headers["Content-Type"] = "application/json";
    }
  }

  let res = await fetch(`${API_BASE}${path}`, {
    ...options,
    method,
    credentials: "include",
    headers,
  });

  // If 401 and not already hitting refresh or a login endpoint, try refresh once
  if (res.status === 401 && !path.includes("/auth/refresh") && !path.includes("/auth/login")) {
    const refreshed = await doRefresh();
    if (refreshed) {
      // Re-read CSRF cookie (refresh may have issued a new one)
      const newCsrf = readCsrfCookie();
      if (isUnsafe(method) && newCsrf) {
        headers["X-CSRF-Token"] = newCsrf;
      }
      res = await fetch(`${API_BASE}${path}`, {
        ...options,
        method,
        credentials: "include",
        headers,
      });
    } else {
      // Refresh failed — throw so caller can clear state
      throw new ApiError(401, "Session expired");
    }
  }

  const body = await parseResponse(res);

  if (!res.ok) {
    throw new ApiError(res.status, extractDetail(body));
  }

  return body as T;
}

// ── Typed helpers ────────────────────────────────────────────────────────────

export const api = {
  get<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
    return request<T>(path, options);
  },

  post<T = unknown>(path: string, data?: unknown): Promise<T> {
    return request<T>(path, {
      method: "POST",
      body: data !== undefined ? JSON.stringify(data) : undefined,
    });
  },

  put<T = unknown>(path: string, data?: unknown): Promise<T> {
    return request<T>(path, {
      method: "PUT",
      body: data !== undefined ? JSON.stringify(data) : undefined,
    });
  },

  patch<T = unknown>(path: string, data?: unknown): Promise<T> {
    return request<T>(path, {
      method: "PATCH",
      body: data !== undefined ? JSON.stringify(data) : undefined,
    });
  },

  delete<T = unknown>(path: string): Promise<T> {
    return request<T>(path, { method: "DELETE" });
  },

  /** Fetch a pre-auth CSRF token from the server. */
  async fetchCsrf(): Promise<string> {
    const res = await fetch(`${API_BASE}/auth/csrf`, {
      credentials: "include",
    });
    if (!res.ok) throw new ApiError(res.status, "Failed to fetch CSRF token");
    const data = (await res.json()) as { csrf_token: string };
    return data.csrf_token;
  },

  /** Get the current user from the session. */
  getMe(): Promise<User> {
    return request<User>("/auth/me");
  },

  /** Register a new account. Requires pre-auth CSRF. */
  register(data: {
    username: string;
    password: string;
    email?: string;
  }): Promise<{ user: User }> {
    return request<{ user: User }>("/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  /** Login. Requires pre-auth CSRF. Sets session cookies. */
  login(data: {
    username: string;
    password: string;
  }): Promise<{ user: User }> {
    return request<{ user: User }>("/auth/login", {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  /** Logout. Requires session-bound CSRF. */
  logout(): Promise<void> {
    return request<void>("/auth/logout", { method: "POST" });
  },
};

// ── Safe redirect helpers ────────────────────────────────────────────────────

export function safeRedirect(path: string): string {
  // Only allow local relative paths starting with /
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("://")) {
    return "/";
  }
  return path;
}

export function getReturnUrl(searchParams: URLSearchParams): string {
  const raw = searchParams.get("return") ?? "/";
  return safeRedirect(raw);
}
